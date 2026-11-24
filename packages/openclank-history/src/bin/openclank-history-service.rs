//! Authenticated single-writer History boundary with native host transport.

use openclank_history::catalog::{ActionRecord, ActionState, BeginResult};
use openclank_history::operations::{
    batch_capture_digest, begin, capture_input_digest, BatchAfterInput, BatchCaptureInput,
    HistoryCoordinator,
};
use openclank_history::protocol::{
    validate as protocol_validate, validate_control as protocol_validate_control,
    BatchCompleteEntry, BatchPrepareEntry, ControlEnvelope, ProtocolError, RegistryBatchMove, RegistrySubtreeEntry, RegistrySubtreeGuard, RequestEnvelope, ResourceHandle,
    ResourceVersionRestorePreview, RestoreDestinationPreview, RestoreEffect, ServiceRequest,
    ServiceResponse,
};
use openclank_history::registry::{ResourceRegistration, ResourceRegistry};
use openclank_history::restore::{
    apply_restore_authorized_durable, prepare_restore_authorized, FilesystemRestoreProvider,
    RestoreProvider, RestoreRequest,
};
use sha2::{Digest, Sha256};
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::collections::{BTreeMap, BTreeSet};

const MAX_FRAME: usize = 1024 * 1024;
const MAX_BATCH_RESOURCES: usize = 1024;
// This wire operation remains unavailable until the Files coordinator and
// its exact recovery path are integrated and reviewed together.
const MANAGED_REGISTRY_BATCH_READY: bool = cfg!(target_os = "linux");
const MAX_BATCH_INLINE_BYTES: usize = 512 * 1024;
const MAX_STAGE_CHUNK: usize = 512 * 1024;
/// Staging is a disk-safety reservation, not a per-file eligibility limit. A
/// provider may upload any size that its configured history budget and the
/// available staging reservation can admit. Operators can tune this bound for
/// a small disk with the environment setting; it is deliberately much larger
/// than the IPC frame size.
const DEFAULT_STAGING_QUOTA_BYTES: u64 = 2 * 1024 * 1024 * 1024;
const DEFAULT_STAGING_TTL_MILLIS: u64 = 15 * 60 * 1000;
const DEFAULT_MAX_ACTIVE_STAGES: usize = 64;

#[derive(Clone, Debug, serde::Deserialize)]
struct ServerCredential {
    actor_id: String,
    account_id: String,
    token: String,
    #[serde(default)]
    owner_accounts: BTreeSet<String>,
    #[serde(default)]
    capabilities: BTreeSet<String>,
    #[serde(default)]
    checkpoint_root_id: Option<String>,
    #[serde(default)]
    drain_session_id: Option<String>,
}

/// Provider roots are assembled by the authenticated app/Files supervisor
/// from its root registry and passed to this process at startup. A request may
/// name only one of these root ids; the path is checked against the same record
/// before it enters the service-owned resource map.
#[derive(Clone, Debug, serde::Deserialize)]
struct AuthorizedHistoryRoot {
    root_id: String,
    canonical_path: String,
    #[serde(default = "default_root_kind")]
    kind: String,
    #[serde(default)]
    account_ids: BTreeSet<String>,
    #[serde(default)]
    workspace_ids: BTreeSet<String>,
    /// Empty preserves assigned-root semantics; trusted host roots bind exact human actors.
    #[serde(default)]
    actor_ids: BTreeSet<String>,
    #[serde(default)] selected_child: Option<String>,
    #[serde(default)] parent_device: Option<u64>,
    #[serde(default)] parent_inode: Option<u64>,
}

fn default_root_kind() -> String {
    "recursive_directory".into()
}

fn restricted_child(root: &AuthorizedHistoryRoot, relative: &str) -> Result<(), String> {
    let Some(child) = root.selected_child.as_deref() else {
        if root.parent_device.is_some() || root.parent_inode.is_some() { return Err("invalid restricted root".into()); }
        return Ok(());
    };
    if root.kind != "recursive_directory" || child.is_empty() || child == "." || child == ".." || child.contains('/') || child.contains('\\') || child.contains('\0') || root.parent_device.is_none() || root.parent_inode.is_none() {
        return Err("invalid restricted root".into());
    }
    #[cfg(unix)] {
        use std::os::unix::fs::MetadataExt;
        let metadata = std::fs::symlink_metadata(&root.canonical_path).map_err(|_| "restricted root unavailable")?;
        if !metadata.file_type().is_dir() || Some(metadata.dev()) != root.parent_device || Some(metadata.ino()) != root.parent_inode { return Err("restricted root changed".into()); }
    }
    #[cfg(not(unix))] { return Err("restricted root unavailable".into()); }
    if relative != child && !relative.strip_prefix(child).is_some_and(|rest| rest.starts_with('/')) { return Err("resource outside selected child".into()); }
    Ok(())
}

fn worktree_read_projection<'a>(action:&'a ActionRecord,version:&str)->Option<(u64,&'a str)> {
    if action.operation=="source_worktree_create_v2" && action.state==ActionState::Complete && action.after_resources.len()==1 {
        let item=&action.after_resources[0];
        if item.resource_key==action.resource_key && item.after.version_id==version && action.after.as_ref()==Some(&item.after) {return Some((item.coverage.byte_len?,item.fingerprint.as_str()));}
    }
    if action.operation=="source_worktree_inverse_v2" && matches!(&action.state,ActionState::BeforeDurable|ActionState::Applying|ActionState::Applied|ActionState::CapturingAfter|ActionState::AfterDurable|ActionState::Complete) && action.before_resources.len()==1 {
        let item=&action.before_resources[0];let receipt=item.before.as_ref()?;
        if item.resource_key==action.resource_key && receipt.version_id==version && action.before.as_ref()==Some(receipt) {return Some((item.coverage.byte_len?,item.fingerprint.as_str()));}
    }
    None
}

fn root_is_exact(root: &AuthorizedHistoryRoot) -> bool {
    root.kind == "exact_file"
}

struct StagedIoPermit {
    active: std::sync::Arc<std::sync::atomic::AtomicUsize>,
}

impl Drop for StagedIoPermit {
    fn drop(&mut self) {
        self.active
            .fetch_sub(1, std::sync::atomic::Ordering::AcqRel);
    }
}

#[derive(Clone, Debug)]
struct StagedUpload {
    checkpoint_slot_not_started: bool,
    stage_file_ready: bool,
    read_file: Option<std::sync::Arc<std::fs::File>>,
    read_version_id: Option<String>,
    read_digest: Option<Sha256>,
    worktree_completion_issued: bool,
    actor_id: String,
    account_id: String,
    action_id: String,
    content_length: u64,
    written: u64,
    fingerprint: String,
    finished: bool,
    last_activity_millis: u64,
    path: std::path::PathBuf,
    io_lock: std::sync::Arc<tokio::sync::Mutex<()>>,
    io_active_count: std::sync::Arc<std::sync::atomic::AtomicUsize>,
}

#[derive(Clone, Debug)]
struct OwnedStagedUpload {
    upload_id: String,
    io_lock: std::sync::Arc<tokio::sync::Mutex<()>>,
}

#[derive(Clone, Debug)]
struct AuthBindings {
    credentials: Vec<ServerCredential>,
    legacy_account: Option<String>,
    legacy_token: Option<String>,
    authority_generation: Option<u64>,
}

#[derive(Clone, Debug, serde::Deserialize)]
struct AuthorityManifest {
    generation: u64,
    credentials: Vec<ServerCredential>,
    #[serde(default)]
    authorized_roots: Vec<AuthorizedHistoryRoot>,
}

impl AuthBindings {
    fn reload_credentials_file(&mut self, path: Option<&std::path::Path>) -> Vec<String> {
        let Some(path) = path else {
            return Vec::new();
        };
        let previous = self
            .credentials
            .iter()
            .map(|binding| binding.account_id.clone())
            .collect::<BTreeSet<_>>();
        let raw = match std::fs::read_to_string(path) {
            Ok(raw) => raw,
            Err(error) => {
                self.credentials.clear();
                let _ = error;
                return previous.into_iter().collect();
            }
        };
        let mut credentials = match serde_json::from_str::<Vec<ServerCredential>>(&raw) {
            Ok(credentials) => credentials,
            Err(_) => {
                self.credentials.clear();
                return previous.into_iter().collect();
            }
        };
        credentials.retain(|binding| {
            !binding.actor_id.trim().is_empty()
                && !binding.account_id.trim().is_empty()
                && !binding.token.trim().is_empty()
        });
        if credentials.is_empty() {
            self.credentials.clear();
            return previous.into_iter().collect();
        }
        let current = credentials
            .iter()
            .map(|binding| binding.account_id.clone())
            .collect::<BTreeSet<_>>();
        self.credentials = credentials;
        previous.difference(&current).cloned().collect()
    }

    fn reload_authority_file(
        &mut self,
        path: Option<&std::path::Path>,
    ) -> (Vec<String>, Vec<AuthorizedHistoryRoot>) {
        let previous = self
            .credentials
            .iter()
            .map(|binding| binding.account_id.clone())
            .collect::<BTreeSet<_>>();
        let Some(path) = path else {
            return (Vec::new(), Vec::new());
        };
        let manifest = std::fs::read_to_string(path)
            .ok()
            .and_then(|raw| serde_json::from_str::<AuthorityManifest>(&raw).ok());
        let Some(manifest) = manifest else {
            self.credentials.clear();
            self.authority_generation = None;
            return (previous.into_iter().collect(), Vec::new());
        };
        self.authority_generation = Some(manifest.generation);
        let mut credentials = manifest.credentials;
        credentials.retain(|binding| {
            !binding.actor_id.trim().is_empty()
                && !binding.account_id.trim().is_empty()
                && !binding.token.trim().is_empty()
        });
        if credentials.is_empty() {
            self.credentials.clear();
            return (previous.into_iter().collect(), Vec::new());
        }
        let current = credentials
            .iter()
            .map(|binding| binding.account_id.clone())
            .collect::<BTreeSet<_>>();
        self.credentials = credentials;
        (
            previous.difference(&current).cloned().collect(),
            manifest.authorized_roots,
        )
    }

    fn grants(&self, legacy_grants: &[(String, String)]) -> Vec<(String, String)> {
        let mut grants = self
            .credentials
            .iter()
            .flat_map(|binding| {
                std::iter::once((binding.actor_id.clone(), binding.account_id.clone())).chain(
                    binding
                        .owner_accounts
                        .iter()
                        .cloned()
                        .map(|owner| (binding.actor_id.clone(), owner)),
                )
            })
            .collect::<Vec<_>>();
        if self.credentials.is_empty() {
            grants.extend(legacy_grants.iter().cloned());
        }
        grants
    }

    fn require(binding: &ServerCredential, capability: &str) -> Result<(), ProtocolError> {
        if binding.capabilities.contains("admin") || binding.capabilities.contains(capability) {
            Ok(())
        } else {
            Err(ProtocolError::AccountMismatch)
        }
    }

    fn authenticate(
        &self,
        auth: &openclank_history::protocol::AuthContext,
    ) -> Result<ServerCredential, ProtocolError> {
        if let Some(binding) = self.credentials.iter().find(|binding| {
            (binding.actor_id == "*" || binding.actor_id == auth.actor_id)
                && binding.account_id == auth.account_id
                && binding.token == auth.token
        }) {
            return Ok(binding.clone());
        }
        if self.credentials.is_empty()
            && self.legacy_account.as_deref() == Some(auth.account_id.as_str())
            && self.legacy_token.as_deref() == Some(auth.token.as_str())
        {
            return Ok(ServerCredential {
                actor_id: auth.actor_id.clone(),
                account_id: auth.account_id.clone(),
                token: auth.token.clone(),
                owner_accounts: BTreeSet::new(),
                capabilities: ["admin".to_owned()].into_iter().collect(),
                checkpoint_root_id: None,
                drain_session_id: None,
            });
        }
        Err(ProtocolError::InvalidToken)
    }

    fn validate_request(
        &self,
        envelope: &RequestEnvelope,
    ) -> Result<ServerCredential, ProtocolError> {
        let binding = self.authenticate(&envelope.auth)?;
        if binding.checkpoint_root_id.is_some() || binding.drain_session_id.is_some() { return Err(ProtocolError::AccountMismatch); }
        protocol_validate(envelope, &binding.account_id, &binding.token)?;
        Ok(binding)
    }

    fn validate_control(
        &self,
        envelope: &ControlEnvelope,
    ) -> Result<ServerCredential, ProtocolError> {
        let binding = self.authenticate(&envelope.auth)?;
        if binding.checkpoint_root_id.is_some() || binding.drain_session_id.is_some() { return Err(ProtocolError::AccountMismatch); }
        protocol_validate_control(envelope, &binding.account_id, &binding.token)?;
        Ok(binding)
    }

    fn validate_request_cap(
        &self,
        envelope: &RequestEnvelope,
        capability: &str,
    ) -> Result<ServerCredential, ProtocolError> {
        let binding = self.validate_request(envelope)?;
        Self::require(&binding, capability)?;
        Ok(binding)
    }

    fn validate_stage(&self, envelope: &ControlEnvelope) -> Result<ServerCredential, ProtocolError> {
        if envelope.action_id.starts_with("memery-component-") {
            self.validate_memery(envelope)?;
            self.validate_control(envelope)
        } else {
            self.validate_control_cap(envelope, "capture")
        }
    }

    fn validate_memery(&self, envelope: &ControlEnvelope) -> Result<(), ProtocolError> {
        let binding = self.validate_control(envelope)?;
        if binding.actor_id != format!("service:memery-history:{}", binding.account_id)
            || binding.actor_id != envelope.auth.actor_id
            || binding.capabilities != BTreeSet::from(["memery-history".to_owned()]) {
            return Err(ProtocolError::AccountMismatch);
        }
        Ok(())
    }

    fn validate_control_cap(
        &self,
        envelope: &ControlEnvelope,
        capability: &str,
    ) -> Result<ServerCredential, ProtocolError> {
        let binding = self.validate_control(envelope)?;
        Self::require(&binding, capability)?;
        Ok(binding)
    }

    fn validate_files_worktree(&self, envelope: &ControlEnvelope, root_id: &str) -> Result<ServerCredential, ProtocolError> {
        let binding=self.authenticate(&envelope.auth)?;
        protocol_validate_control(envelope,&binding.account_id,&binding.token)?;
        if binding.actor_id=="*" || binding.actor_id.starts_with("agent:") || binding.actor_id!=envelope.auth.actor_id
            || binding.checkpoint_root_id.as_deref()!=Some(root_id)
            || binding.capabilities!=BTreeSet::from(["worktree-capture".to_owned()]) {return Err(ProtocolError::AccountMismatch);}
        Ok(binding)
    }

    fn validate_checkpoint(&self, envelope: &ControlEnvelope, root_id: &str) -> Result<ServerCredential, ProtocolError> {
        let binding = self.authenticate(&envelope.auth)?;
        protocol_validate_control(envelope, &binding.account_id, &binding.token)?;
        if binding.actor_id == "*" || binding.actor_id != envelope.auth.actor_id
            || binding.checkpoint_root_id.as_deref() != Some(root_id)
            || binding.capabilities != BTreeSet::from(["checkpoint-capture".to_owned()]) {
            return Err(ProtocolError::AccountMismatch);
        }
        Ok(binding)
    }
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

fn valid_upload_id(upload_id: &str) -> bool {
    upload_id.len() <= 128
        && !upload_id.is_empty()
        && !upload_id
            .chars()
            .any(|character| character.is_whitespace() || matches!(character, '/' | '\\' | '\0'))
}

fn staged_fingerprint_valid(fingerprint: &str, content_length: u64) -> bool {
    let Some(rest) = fingerprint.strip_prefix("sha256:") else {
        return false;
    };
    let Some((digest, length)) = rest.rsplit_once(':') else {
        return false;
    };
    digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        && length.parse::<u64>().ok() == Some(content_length)
}

fn protected_staged_id(id:&str)->bool {id.starts_with("odysseus-checkpoint-") || id.starts_with("odysseus-worktree-")}

fn staged_upload_path(root: &std::path::Path, upload_id: &str) -> std::path::PathBuf {
    root.join(format!(".openclank-stage-{upload_id}.part"))
}

fn remove_staged_upload(upload: &StagedUpload, staged_bytes: &std::sync::atomic::AtomicU64) {
    let _ = std::fs::remove_file(&upload.path);
    staged_bytes.fetch_sub(upload.content_length, std::sync::atomic::Ordering::AcqRel);
}

async fn remove_owned_staged_uploads(
    staged_uploads: &std::sync::Arc<tokio::sync::Mutex<BTreeMap<String, StagedUpload>>>,
    staged_bytes: &std::sync::atomic::AtomicU64,
    actor_id: &str,
    account_id: &str,
    action_id: &str,
    uploads_to_remove: &[OwnedStagedUpload],
) {
    for captured in uploads_to_remove {
        if protected_staged_id(&captured.upload_id) {continue;}
        let _io_guard = captured.io_lock.lock().await;
        let mut uploads = staged_uploads.lock().await;
        if let Some(upload) = uploads.get(&captured.upload_id) {
            if upload.actor_id == actor_id
                && upload.account_id == account_id
                && upload.action_id == action_id
                && std::sync::Arc::ptr_eq(&upload.io_lock, &captured.io_lock)
            {
                let upload = uploads.remove(&captured.upload_id).expect("upload exists");
                remove_staged_upload(&upload, staged_bytes);
            }
        }
    }
}

async fn remove_checkpoint_stages_checked(
    staged_uploads: &std::sync::Arc<tokio::sync::Mutex<BTreeMap<String, StagedUpload>>>,
    staged_bytes: &std::sync::atomic::AtomicU64, actor_id: &str, account_id: &str,
    action_id: &str, owned: &[OwnedStagedUpload],
) -> Result<(), String> {
    for captured in owned {
        let _guard=captured.io_lock.lock().await;
        let mut uploads=staged_uploads.lock().await;
        let upload=uploads.get(&captured.upload_id).ok_or("checkpoint stage unavailable")?;
        if upload.actor_id!=actor_id || upload.account_id!=account_id || upload.action_id!=action_id
            || !std::sync::Arc::ptr_eq(&upload.io_lock,&captured.io_lock) {return Err("checkpoint stage changed".into());}
        std::fs::remove_file(&upload.path).map_err(|_|"checkpoint stage cleanup unavailable")?;
        let removed=uploads.remove(&captured.upload_id).ok_or("checkpoint stage unavailable")?;
        staged_bytes.fetch_sub(removed.content_length,std::sync::atomic::Ordering::AcqRel);
    }
    Ok(())
}

async fn owned_staged_ids_for_entries(
    staged_uploads: &std::sync::Arc<tokio::sync::Mutex<BTreeMap<String, StagedUpload>>>,
    entries: &[BatchPrepareEntry],
    actor_id: &str,
    account_id: &str,
    action_id: &str,
) -> Vec<OwnedStagedUpload> {
    let uploads = staged_uploads.lock().await;
    let ids = entries
        .iter()
        .filter_map(|entry| entry.staged_upload_id.as_ref())
        .filter(|upload_id| !protected_staged_id(upload_id))
        .filter_map(|upload_id| {
            uploads.get(upload_id).and_then(|upload| {
                (upload.account_id == account_id
                    && upload.actor_id == actor_id
                    && upload.action_id == action_id)
                    .then(|| OwnedStagedUpload {
                        upload_id: upload_id.clone(),
                        io_lock: upload.io_lock.clone(),
                    })
            })
        })
        .filter(|captured| {
            uploads.get(&captured.upload_id).is_some_and(|upload| {
                upload.account_id == account_id
                    && upload.actor_id == actor_id
                    && upload.action_id == action_id
            })
        })
        .collect::<Vec<_>>();
    let mut unique = BTreeSet::new();
    ids.into_iter()
        .filter(|captured| unique.insert(captured.upload_id.clone()))
        .collect()
}

async fn read_owned_staged_upload(
    staged_uploads: &std::sync::Arc<tokio::sync::Mutex<BTreeMap<String, StagedUpload>>>,
    captured: &OwnedStagedUpload,
    actor_id: &str,
    account_id: &str,
    action_id: &str,
    expected_fingerprint: &str,
) -> Result<bytes::Bytes, String> {
    let (upload, io_lock, io_active_count) = {
        let uploads = staged_uploads.lock().await;
        let Some(upload) = uploads.get(&captured.upload_id) else {
            return Err("staged batch upload was replaced or removed".into());
        };
        if upload.actor_id != actor_id
            || upload.account_id != account_id
            || upload.action_id != action_id
            || !std::sync::Arc::ptr_eq(&upload.io_lock, &captured.io_lock)
        {
            return Err("staged batch upload was replaced or removed".into());
        }
        upload
            .io_active_count
            .fetch_add(1, std::sync::atomic::Ordering::AcqRel);
        (
            upload.clone(),
            upload.io_lock.clone(),
            upload.io_active_count.clone(),
        )
    };
    let io_permit = StagedIoPermit {
        active: io_active_count,
    };
    let io_guard = io_lock.lock_owned().await;
    {
        let uploads = staged_uploads.lock().await;
        let Some(current) = uploads.get(&captured.upload_id) else {
            return Err("staged batch upload was replaced or removed".into());
        };
        if current.actor_id != actor_id
            || current.account_id != account_id
            || current.action_id != action_id
            || !std::sync::Arc::ptr_eq(&current.io_lock, &captured.io_lock)
        {
            return Err("staged batch upload was replaced or removed".into());
        }
    }
    if !upload.finished {
        return Err("staged batch upload is incomplete".into());
    }
    if upload.fingerprint != expected_fingerprint {
        return Err("staged batch identity or fingerprint mismatch".into());
    }
    let verification = tokio::task::spawn_blocking(move || {
        #[cfg(all(unix, feature = "test_faults"))]
        qualification_stage_io_delay();
        let result = verify_staged_upload(&upload)
            .and_then(|_| std::fs::read(&upload.path).map_err(|error| error.to_string()))
            .map(bytes::Bytes::from);
        (result, io_guard, io_permit)
    })
    .await
    .map_err(|error| format!("staged batch read task failed: {error}"))?;
    verification.0
}

fn reap_staged_uploads(
    uploads: &mut BTreeMap<String, StagedUpload>,
    now_millis: u64,
    ttl_millis: u64,
    staged_bytes: &std::sync::atomic::AtomicU64,
) {
    let stale = uploads
        .iter()
        .filter(|(upload_id, upload)| {
            (!protected_staged_id(upload_id) || upload_id.starts_with("odysseus-worktree-read-") || upload.worktree_completion_issued || !upload.stage_file_ready)
                && now_millis.saturating_sub(upload.last_activity_millis) >= ttl_millis
                && upload
                    .io_active_count
                    .load(std::sync::atomic::Ordering::Acquire)
                    == 0
        })
        .map(|(upload_id, _)| upload_id.clone())
        .collect::<Vec<_>>();
    for upload_id in stale {
        if let Some(upload) = uploads.remove(&upload_id) {
            if upload.stage_file_ready {remove_staged_upload(&upload, staged_bytes);}
        }
    }
}

fn verify_staged_upload(upload: &StagedUpload) -> Result<(), String> {
    if !upload.stage_file_ready {return Err("staged upload unavailable".into());}
    let metadata = std::fs::metadata(&upload.path).map_err(|error| error.to_string())?;
    if metadata.len() != upload.content_length || upload.written != upload.content_length {
        return Err("staged upload length changed before finish".into());
    }
    // Every staged payload has the form
    // sha256:<lowercase-hex>:<decimal-byte-length>. Inline legacy requests may
    // still carry opaque hints, but staging is a new protocol and never gets
    // to consume bytes without a service-side cryptographic check.
    let Some(rest) = upload.fingerprint.strip_prefix("sha256:") else {
        return Err("staged upload fingerprint must be sha256:<digest>:<length>".into());
    };
    let (expected_hex, expected_length) = rest
        .rsplit_once(':')
        .ok_or_else(|| "staged upload fingerprint is malformed".to_owned())?;
    let expected_length = expected_length
        .parse::<u64>()
        .map_err(|_| "staged upload fingerprint length is malformed".to_owned())?;
    if expected_length != upload.content_length || expected_hex.len() != 64 {
        return Err("staged upload fingerprint length does not match payload".into());
    }
    if !expected_hex
        .bytes()
        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("staged upload fingerprint digest is malformed".into());
    }
    let mut file = std::fs::File::open(&upload.path).map_err(|error| error.to_string())?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 128 * 1024];
    loop {
        use std::io::Read;
        let read = file.read(&mut buffer).map_err(|error| error.to_string())?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    let actual_hex = format!("{:x}", digest.finalize());
    if actual_hex != expected_hex.to_ascii_lowercase() {
        return Err("staged upload fingerprint does not match staged bytes".into());
    }
    Ok(())
}

#[cfg(all(unix, feature = "test_faults"))]
fn qualification_stage_io_delay() {
    let Some(milliseconds) = std::env::var("OPENCLANK_HISTORY_TEST_STAGE_IO_DELAY_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| *value > 0)
    else {
        return;
    };
    std::thread::sleep(std::time::Duration::from_millis(milliseconds));
}

fn staging_budget_error(
    coordinator: &HistoryCoordinator,
    staging_root: &std::path::Path,
    requested: u64,
    currently_staged: u64,
) -> Result<(), String> {
    let policy = coordinator
        .policy_set()
        .map_err(|error| format!("history budget could not be read: {error}"))?;
    // Staging is short-lived and budgeted. The operation envelope determines
    // whether it can publish a backup or only verify required mutation recovery.
    let usage = coordinator
        .usage()
        .map_err(|error| format!("history usage could not be read: {error}"))?;
    let used = usage
        .physical_allocated_bytes
        .saturating_add(usage.reserved_inflight_bytes)
        .saturating_add(currently_staged);
    let available = policy.global.total_bytes.saturating_sub(used);
    if requested > available {
        return Err(format!(
            "history_paused_budget: staged payload exceeds remaining history budget ({available} bytes available)"
        ));
    }
    // Leave a small operational reserve for the catalog and the next atomic
    // rename. This check is only a disk admission guard; it does not make a
    // permanent file-size eligibility rule.
    let free = openclank_history::platform::available_bytes(staging_root).ok_or("history_paused_disk: available disk capacity could not be established")?;
    let free_for_stage = free.saturating_sub(64 * 1024 * 1024);
    if requested > free_for_stage {
        return Err(format!(
            "history_paused_disk: staged payload exceeds available staging capacity ({free_for_stage} bytes available)"
        ));
    }
    Ok(())
}

async fn handle_staging_fast(
    request: ServiceRequest,
    auth_bindings: &AuthBindings,
    coordinator: &std::sync::Arc<tokio::sync::Mutex<HistoryCoordinator>>,
    staging_root: &std::path::Path,
    staging_quota: u64,
    staging_ttl_millis: u64,
    max_active_stages: usize,
    staged_uploads: &std::sync::Arc<tokio::sync::Mutex<BTreeMap<String, StagedUpload>>>,
    staged_bytes: &std::sync::Arc<std::sync::atomic::AtomicU64>,
    staging_admission: &std::sync::Arc<tokio::sync::Mutex<()>>,
    grants: &[(String, String)],
) -> Option<ServiceResponse> {
    match request {
        ServiceRequest::StageBegin {
            envelope,
            upload_id,
            content_length,
            fingerprint,
        } => {
            let response = match auth_bindings
                .validate_stage(&envelope)
                .map_err(|error| format!("{error:?}"))
            {
                Ok(_) if !valid_upload_id(&upload_id) => ServiceResponse::Error {
                    code: "invalid staged upload id".into(),
                },
                Ok(_) if !staged_fingerprint_valid(&fingerprint, content_length) => {
                    ServiceResponse::Error {
                        code: "staged upload fingerprint must be sha256:<digest>:<length>".into(),
                    }
                }
                Ok(_) if content_length > staging_quota => ServiceResponse::Error {
                    code: "staged upload exceeds configured staging quota".into(),
                },
                Ok(_) => {
                    // Serialize the budget snapshot with the reservation increment. The
                    // coordinator is held only for the short usage read; all staged file writes
                    // and hashing happen after this admission guard is released.
                    let _admission_guard = staging_admission.lock().await;
                    {
                        let mut uploads = staged_uploads.lock().await;
                        reap_staged_uploads(
                            &mut uploads,
                            registry_timestamp(),
                            staging_ttl_millis,
                            staged_bytes,
                        );
                    }
                    // Consume the original in-memory claim slot before any file
                    // creation. Failure cannot re-arm it, including after restart.
                    let protected = protected_staged_id(&upload_id);
                    if protected {
                        let mut uploads=staged_uploads.lock().await;
                        let slot=uploads.get_mut(&upload_id);
                        match slot {
                            Some(slot) if slot.checkpoint_slot_not_started
                                && slot.actor_id==envelope.auth.actor_id && slot.account_id==envelope.auth.account_id
                                && slot.action_id==envelope.action_id && slot.fingerprint==fingerprint
                                && slot.content_length==content_length => {slot.checkpoint_slot_not_started=false;}
                            _ => return Some(ServiceResponse::Error {code:"checkpoint stage unavailable".into()}),
                        }
                    }
                    let budget = {
                        let coordinator = coordinator.lock().await;
                        staging_budget_error(
                            &coordinator,
                            staging_root,
                            content_length,
                            staged_bytes.load(std::sync::atomic::Ordering::Acquire),
                        )
                    };
                    if let Err(error) = budget {
                        ServiceResponse::Error { code: error }
                    } else {
                        let mut uploads = staged_uploads.lock().await;
                        if !protected && uploads.len() >= max_active_stages {
                            ServiceResponse::Error {
                                code: "staged upload concurrency quota is full".into(),
                            }
                        } else if !protected && uploads.contains_key(&upload_id) {
                            ServiceResponse::Error {
                                code: "staged upload id is already active".into(),
                            }
                        } else {
                            let admitted = staged_bytes
                                .fetch_update(
                                    std::sync::atomic::Ordering::AcqRel,
                                    std::sync::atomic::Ordering::Acquire,
                                    |used| {
                                        used.checked_add(content_length)
                                            .filter(|next| *next <= staging_quota)
                                    },
                                )
                                .is_ok();
                            if !admitted {
                                ServiceResponse::Error {
                                    code: "staged upload exceeds configured staging quota".into(),
                                }
                            } else {
                                let path = staged_upload_path(staging_root, &upload_id);
                                match std::fs::OpenOptions::new()
                                    .write(true)
                                    .create_new(true)
                                    .open(&path)
                                {
                                    Ok(file) => {
                                        #[cfg(unix)]
                                        {
                                                                                    let _ = file.set_permissions(
                                                std::fs::Permissions::from_mode(0o600),
                                            );
                                        }
                                        let now = registry_timestamp();
                                        uploads.insert(
                                            upload_id,
                                            StagedUpload {
                                                read_file:None, read_version_id:None, read_digest:None, worktree_completion_issued:false,
                                                checkpoint_slot_not_started: false,
                                                stage_file_ready: true,
                                                actor_id: envelope.auth.actor_id,
                                                account_id: envelope.auth.account_id,
                                                action_id: envelope.action_id,
                                                content_length,
                                                written: 0,
                                                fingerprint,
                                                finished: false,
                                                last_activity_millis: now,
                                                path,
                                                io_lock: std::sync::Arc::new(
                                                    tokio::sync::Mutex::new(()),
                                                ),
                                                io_active_count: std::sync::Arc::new(
                                                    std::sync::atomic::AtomicUsize::new(0),
                                                ),
                                            },
                                        );
                                        ServiceResponse::Accepted
                                    }
                                    Err(error) => {
                                        staged_bytes.fetch_sub(
                                            content_length,
                                            std::sync::atomic::Ordering::AcqRel,
                                        );
                                        ServiceResponse::Error {
                                            code: format!("staged upload could not start: {error}"),
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                Err(error) => ServiceResponse::Error { code: error },
            };
            Some(response)
        }
        ServiceRequest::StageChunk {
            envelope,
            upload_id,
            offset,
            content,
        } => {
            let response = match auth_bindings
                .validate_stage(&envelope)
                .map_err(|error| format!("{error:?}"))
            {
                Ok(_) => match content {
                    None => ServiceResponse::Error {
                        code: "staged upload chunk is missing".into(),
                    },
                    Some(content) if content.len() > MAX_STAGE_CHUNK => ServiceResponse::Error {
                        code: "staged upload chunk is too large".into(),
                    },
                    Some(content) => {
                        let chunk_len = content.len() as u64;
                        let (path, expected_length, io_lock, io_active_count) = {
                            let uploads = staged_uploads.lock().await;
                            match uploads.get(&upload_id) {
                                None => {
                                    return Some(ServiceResponse::Error {
                                        code: "staged upload is unknown".into(),
                                    });
                                }
                                Some(upload)
                                    if upload.account_id != envelope.auth.account_id
                                        || upload.actor_id != envelope.auth.actor_id
                                        || upload.action_id != envelope.action_id =>
                                {
                                    return Some(ServiceResponse::Error {
                                        code: "staged upload identity mismatch".into(),
                                    });
                                }
                                Some(upload) if !upload.stage_file_ready || upload.finished || offset != upload.written => {
                                    return Some(ServiceResponse::Error {
                                        code: "staged upload offset is invalid".into(),
                                    });
                                }
                                Some(upload)
                                    if upload.written.saturating_add(content.len() as u64)
                                        > upload.content_length =>
                                {
                                    return Some(ServiceResponse::Error {
                                        code: "staged upload exceeds declared length".into(),
                                    });
                                }
                                Some(upload) => {
                                    upload
                                        .io_active_count
                                        .fetch_add(1, std::sync::atomic::Ordering::AcqRel);
                                    (
                                        upload.path.clone(),
                                        upload.content_length,
                                        upload.io_lock.clone(),
                                        upload.io_active_count.clone(),
                                    )
                                }
                            }
                        };
                        // Different uploads proceed concurrently; only chunks belonging to one
                        // upload share a lock, which preserves offset order without making a
                        // slow fsync/hash stall unrelated callers or Health.
                        let io_permit = StagedIoPermit {
                            active: io_active_count,
                        };
                        let io_guard = io_lock.clone().lock_owned().await;
                        {
                            let uploads = staged_uploads.lock().await;
                            match uploads.get(&upload_id) {
                                Some(upload)
                                    if upload.account_id == envelope.auth.account_id
                                        && upload.actor_id == envelope.auth.actor_id
                                        && upload.action_id == envelope.action_id
                                        && !upload.finished
                                        && upload.written == offset => {}
                                Some(_) => {
                                    return Some(ServiceResponse::Error {
                                        code: "staged upload offset is invalid".into(),
                                    });
                                }
                                None => {
                                    return Some(ServiceResponse::Error {
                                        code: "staged upload is unknown".into(),
                                    });
                                }
                            }
                        }
                        let append = tokio::task::spawn_blocking(move || {
                            let result = (|| {
                                #[cfg(all(unix, feature = "test_faults"))]
                                qualification_stage_io_delay();
                                let mut file = std::fs::OpenOptions::new()
                                    .append(true)
                                    .open(&path)
                                    .map_err(|error| error.to_string())?;
                                use std::io::Write;
                                file.write_all(&content)
                                    .map_err(|error| error.to_string())?;
                                file.sync_data().map_err(|error| error.to_string())?;
                                Ok::<(), String>(())
                            })();
                            Ok::<_, String>((result, io_guard, io_permit))
                        })
                        .await;
                        match append {
                            Ok(Ok((Ok(()), _io_guard_guard, _io_permit_guard))) => {
                                let mut uploads = staged_uploads.lock().await;
                                match uploads.get_mut(&upload_id) {
                                    Some(upload)
                                        if upload.account_id == envelope.auth.account_id
                                            && upload.actor_id == envelope.auth.actor_id
                                            && upload.action_id == envelope.action_id
                                            && !upload.finished
                                            && upload.written == offset =>
                                    {
                                        let observed_len = std::fs::metadata(&upload.path)
                                            .map(|metadata| metadata.len());
                                        if observed_len.ok()
                                            != Some(offset.saturating_add(chunk_len))
                                        {
                                            ServiceResponse::Error {
                                                code: "staged upload length changed during append"
                                                    .into(),
                                            }
                                        } else if offset.saturating_add(chunk_len) > expected_length
                                        {
                                            ServiceResponse::Error {
                                                code: "staged upload exceeds declared length"
                                                    .into(),
                                            }
                                        } else {
                                            upload.written = offset.saturating_add(chunk_len);
                                            upload.last_activity_millis = registry_timestamp();
                                            ServiceResponse::Accepted
                                        }
                                    }
                                    Some(_) => ServiceResponse::Error {
                                        code: "staged upload changed during append".into(),
                                    },
                                    None => ServiceResponse::Error {
                                        code: "staged upload is unknown".into(),
                                    },
                                }
                            }
                            Ok(Ok((Err(error), _io_guard_guard, _io_permit_guard))) => {
                                ServiceResponse::Error {
                                    code: format!("staged upload write failed: {error}"),
                                }
                            }
                            Ok(Err(error)) => ServiceResponse::Error {
                                code: format!("staged upload write task failed: {error}"),
                            },
                            Err(error) => ServiceResponse::Error {
                                code: format!("staged upload write task failed: {error}"),
                            },
                        }
                    }
                },
                Err(error) => ServiceResponse::Error { code: error },
            };
            Some(response)
        }
        ServiceRequest::StageFinish {
            envelope,
            upload_id,
        } => {
            let response = match auth_bindings
                .validate_stage(&envelope)
                .map_err(|error| format!("{error:?}"))
            {
                Ok(_) => {
                    let staged = {
                        let uploads = staged_uploads.lock().await;
                        uploads.get(&upload_id).map(|upload| {
                            upload
                                .io_active_count
                                .fetch_add(1, std::sync::atomic::Ordering::AcqRel);
                            upload.clone()
                        })
                    };
                    let io_permit = staged.as_ref().map(|upload| StagedIoPermit {
                        active: upload.io_active_count.clone(),
                    });
                    match staged {
                        None => ServiceResponse::Error {
                            code: "staged upload is unknown".into(),
                        },
                        Some(upload)
                            if upload.account_id != envelope.auth.account_id
                                || upload.actor_id != envelope.auth.actor_id
                                || upload.action_id != envelope.action_id =>
                        {
                            ServiceResponse::Error {
                                code: "staged upload identity mismatch".into(),
                            }
                        }
                        Some(upload) if !upload.stage_file_ready || upload.written != upload.content_length => {
                            ServiceResponse::Error {
                                code: "staged upload is incomplete".into(),
                            }
                        }
                        Some(upload) => {
                            let io_guard = upload.io_lock.clone().lock_owned().await;
                            let io_permit = io_permit.expect("staged permit exists");
                            let verification_upload = upload.clone();
                            let verification = tokio::task::spawn_blocking(move || {
                                #[cfg(all(unix, feature = "test_faults"))]
                                qualification_stage_io_delay();
                                let result = verify_staged_upload(&verification_upload);
                                (result, io_guard, io_permit)
                            })
                            .await;
                            let mut _io_guard_guard = None;
                            let mut _io_permit_guard = None;
                            let verification = match verification {
                                Ok((result, guard, permit)) => {
                                    _io_guard_guard = Some(guard);
                                    _io_permit_guard = Some(permit);
                                    result
                                }
                                Err(error) => {
                                    Err(format!("staged upload verification task failed: {error}"))
                                }
                            };
                            match verification {
                                Ok(()) => {
                                    let mut uploads = staged_uploads.lock().await;
                                    match uploads.get_mut(&upload_id) {
                                        Some(current)
                                            if current.account_id == envelope.auth.account_id
                                                && current.actor_id == envelope.auth.actor_id
                                                && current.action_id == envelope.action_id
                                                && !current.finished
                                                && current.written == current.content_length =>
                                        {
                                            current.finished = true;
                                            current.last_activity_millis = registry_timestamp();
                                            ServiceResponse::Staged {
                                                upload_id,
                                                content_length: current.content_length,
                                            }
                                        }
                                        Some(_) => ServiceResponse::Error {
                                            code: "staged upload changed during verification"
                                                .into(),
                                        },
                                        None => ServiceResponse::Error {
                                            code: "staged upload is unknown".into(),
                                        },
                                    }
                                }
                                Err(error) => ServiceResponse::Error { code: error },
                            }
                        }
                    }
                }
                Err(error) => ServiceResponse::Error { code: error },
            };
            Some(response)
        }
        ServiceRequest::StageAbort {
            envelope,
            upload_id,
        } => {
            if protected_staged_id(&upload_id) {
                return Some(ServiceResponse::Error { code: "protected stage unavailable".into() });
            }
            let response = match auth_bindings
                .validate_stage(&envelope)
                .map_err(|error| format!("{error:?}"))
            {
                Ok(_) => {
                    let upload_lock = {
                        let uploads = staged_uploads.lock().await;
                        uploads.get(&upload_id).map(|upload| upload.io_lock.clone())
                    };
                    let Some(upload_lock) = upload_lock else {
                        return Some(ServiceResponse::Accepted);
                    };
                    let _io_guard = upload_lock.lock().await;
                    let mut uploads = staged_uploads.lock().await;
                    match uploads.get(&upload_id) {
                        Some(upload)
                            if upload.account_id == envelope.auth.account_id
                                && upload.actor_id == envelope.auth.actor_id
                                && upload.action_id == envelope.action_id =>
                        {
                            let upload = uploads.remove(&upload_id).expect("upload exists");
                            remove_staged_upload(&upload, staged_bytes);
                            ServiceResponse::Accepted
                        }
                        Some(_) => ServiceResponse::Error {
                            code: "staged upload identity mismatch".into(),
                        },
                        None => ServiceResponse::Accepted,
                    }
                }
                Err(error) => ServiceResponse::Error { code: error },
            };
            Some(response)
        }
        ServiceRequest::PrepareStaged {
            envelope,
            upload_id,
            fingerprint,
        } => {
            let response = match auth_bindings
                .validate_request_cap(&envelope, "capture")
                .map_err(|error| format!("{error:?}"))
            {
                Ok(_)
                    if !request_resources_allowed(
                        &envelope.request,
                        &envelope.auth.actor_id,
                        &envelope.auth.account_id,
                        grants,
                    ) =>
                {
                    ServiceResponse::Error {
                        code: "AccountMismatch".into(),
                    }
                }
                Ok(_) => {
                    let staged = {
                        let uploads = staged_uploads.lock().await;
                        uploads.get(&upload_id).map(|upload| {
                            upload
                                .io_active_count
                                .fetch_add(1, std::sync::atomic::Ordering::AcqRel);
                            upload.clone()
                        })
                    };
                    let io_permit = staged.as_ref().map(|upload| StagedIoPermit {
                        active: upload.io_active_count.clone(),
                    });
                    match staged {
                        None => ServiceResponse::Error {
                            code: "staged upload is unknown".into(),
                        },
                        Some(staged)
                            if staged.account_id != envelope.auth.account_id
                                || staged.actor_id != envelope.auth.actor_id
                                || staged.action_id != envelope.request.action_id
                                || !staged.finished
                                || staged.fingerprint != fingerprint =>
                        {
                            ServiceResponse::Error {
                                code: "staged upload identity or fingerprint mismatch".into(),
                            }
                        }
                        Some(staged) => {
                            let io_guard = staged.io_lock.clone().lock_owned().await;
                            let io_permit = io_permit.expect("staged permit exists");
                            let path = staged.path.clone();
                            let verification_upload = staged.clone();
                            let verification = tokio::task::spawn_blocking(move || {
                                #[cfg(all(unix, feature = "test_faults"))]
                                qualification_stage_io_delay();
                                let result = (|| {
                                    verify_staged_upload(&verification_upload)?;
                                    std::fs::read(path).map_err(|error| error.to_string())
                                })();
                                Ok::<_, String>((result, io_guard, io_permit))
                            })
                            .await;
                            let mut _io_guard_guard = None;
                            let mut _io_permit_guard = None;
                            let verification = match verification {
                                Ok(Ok((result, guard, permit))) => {
                                    _io_guard_guard = Some(guard);
                                    _io_permit_guard = Some(permit);
                                    result
                                }
                                Ok(Err(error)) => Err(error),
                                Err(error) => Err(format!("staged read task failed: {error}")),
                            };
                            let result = match verification {
                                Ok(bytes) => {
                                    let coordinator = coordinator.lock().await;
                                    match begin(coordinator.catalog(), envelope.request.clone()) {
                                        Ok(BeginResult::Existing(record))
                                            if !matches!(
                                                record.state,
                                                ActionState::Intent | ActionState::CaptureFailed
                                            ) =>
                                        {
                                            let digest =
                                                capture_input_digest(Some(&bytes), &fingerprint);
                                            if record.before_capture_digest.as_deref()
                                                == Some(digest.as_str())
                                            {
                                                Ok(record)
                                            } else {
                                                Err("capture idempotency conflict".into())
                                            }
                                        }
                                        Ok(_) => coordinator
                                            .capture_before(
                                                &envelope.request.action_id,
                                                Some(bytes::Bytes::from(bytes)),
                                                staged.fingerprint.clone(),
                                            )
                                            .await
                                            .map_err(|error| error.to_string()),
                                        Err(error) => Err(error.to_string()),
                                    }
                                }
                                Err(error) => Err(format!("staged read task failed: {error}")),
                            };
                            let removed = {
                                let mut uploads = staged_uploads.lock().await;
                                uploads.remove(&upload_id)
                            };
                            if let Some(upload) = removed.as_ref() {
                                remove_staged_upload(upload, staged_bytes);
                            }
                            match result {
                                Ok(record) => ServiceResponse::Action(record),
                                Err(error) => ServiceResponse::Error {
                                    code: format!("staged before capture failed: {error}"),
                                },
                            }
                        }
                    }
                }
                Err(error) => ServiceResponse::Error { code: error },
            };
            Some(response)
        }
        ServiceRequest::CompleteStaged {
            envelope,
            upload_id,
            fingerprint,
        } => {
            let response = match auth_bindings
                .validate_control_cap(&envelope, "capture")
                .map_err(|error| format!("{error:?}"))
            {
                Ok(_) => {
                    let authorized = {
                        let coordinator = coordinator.lock().await;
                        authorize(&coordinator, &envelope, &envelope.auth.account_id, grants)
                            .map(|_| ())
                    };
                    match authorized {
                        Err(error) => ServiceResponse::Error { code: error },
                        Ok(()) => {
                            let staged = {
                                let uploads = staged_uploads.lock().await;
                                uploads.get(&upload_id).map(|upload| {
                                    upload
                                        .io_active_count
                                        .fetch_add(1, std::sync::atomic::Ordering::AcqRel);
                                    upload.clone()
                                })
                            };
                            let io_permit = staged.as_ref().map(|upload| StagedIoPermit {
                                active: upload.io_active_count.clone(),
                            });
                            match staged {
                                None => ServiceResponse::Error {
                                    code: "staged upload is unknown".into(),
                                },
                                Some(staged)
                                    if staged.account_id != envelope.auth.account_id
                                        || staged.actor_id != envelope.auth.actor_id
                                        || staged.action_id != envelope.action_id
                                        || !staged.finished
                                        || staged.fingerprint != fingerprint =>
                                {
                                    ServiceResponse::Error {
                                        code: "staged upload identity or fingerprint mismatch"
                                            .into(),
                                    }
                                }
                                Some(staged) => {
                                    let io_guard = staged.io_lock.clone().lock_owned().await;
                                    let io_permit = io_permit.expect("staged permit exists");
                                    let path = staged.path.clone();
                                    let verification_upload = staged.clone();
                                    let verification = tokio::task::spawn_blocking(move || {
                                        #[cfg(all(unix, feature = "test_faults"))]
                                        qualification_stage_io_delay();
                                        let result = (|| {
                                            verify_staged_upload(&verification_upload)?;
                                            std::fs::read(path).map_err(|error| error.to_string())
                                        })();
                                        Ok::<_, String>((result, io_guard, io_permit))
                                    })
                                    .await;
                                    let mut _io_guard_guard = None;
                                    let mut _io_permit_guard = None;
                                    let verification = match verification {
                                        Ok(Ok((result, guard, permit))) => {
                                            _io_guard_guard = Some(guard);
                                            _io_permit_guard = Some(permit);
                                            result
                                        }
                                        Ok(Err(error)) => Err(error),
                                        Err(error) => {
                                            Err(format!("staged read task failed: {error}"))
                                        }
                                    };
                                    let result = match verification {
                                        Ok(bytes) => coordinator
                                            .lock()
                                            .await
                                            .capture_after(
                                                &envelope.action_id,
                                                Some(bytes::Bytes::from(bytes)),
                                                staged.fingerprint.clone(),
                                            )
                                            .await
                                            .map_err(|error| error.to_string()),
                                        Err(error) => {
                                            Err(format!("staged read task failed: {error}"))
                                        }
                                    };
                                    let removed = {
                                        let mut uploads = staged_uploads.lock().await;
                                        uploads.remove(&upload_id)
                                    };
                                    if let Some(upload) = removed.as_ref() {
                                        remove_staged_upload(upload, staged_bytes);
                                    }
                                    match result {
                                        Ok(record) => ServiceResponse::Action(record),
                                        Err(error) => ServiceResponse::Error {
                                            code: format!("staged after capture failed: {error}"),
                                        },
                                    }
                                }
                            }
                        }
                    }
                }
                Err(error) => ServiceResponse::Error { code: error },
            };
            Some(response)
        }
        _ => None,
    }
}

async fn read_frame<R: tokio::io::AsyncBufRead + Unpin>(
    reader: &mut R,
) -> std::io::Result<Option<Vec<u8>>> {
    use tokio::io::AsyncBufReadExt;
    let mut frame = Vec::new();
    loop {
        let buf = reader.fill_buf().await?;
        if buf.is_empty() {
            return Ok(if frame.is_empty() { None } else { Some(frame) });
        }
        let newline = buf.iter().position(|byte| *byte == b'\n');
        let take = newline.map_or(buf.len(), |index| index + 1);
        if frame.len() + take > MAX_FRAME {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "frame too large",
            ));
        }
        frame.extend_from_slice(&buf[..take]);
        reader.consume(take);
        if newline.is_some() {
            frame.pop();
            return Ok(Some(frame));
        }
    }
}

fn owner_allowed(owner: &str, actor: &str, account: &str, grants: &[(String, String)]) -> bool {
    owner == account
        || grants
            .iter()
            .any(|(granted_actor, granted_owner)| granted_actor == actor && granted_owner == owner)
}

/// Regular-file restore through a removed (inactive) registration: only into an absent leaf.
fn regular_restore_state_allowed(active: bool, observed: &openclank_history::restore::RegularFileObservationV1) -> bool {
    active || matches!(observed.state, openclank_history::restore::RegularFileStateV1::Absent)
}

fn registered_files_resource_key(
    resource_id: &str,
    auth: &openclank_history::protocol::AuthContext,
    grants: &[(String, String)],
    resource_map: &std::path::Path,
    host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
    allow_inactive_history: bool,
) -> Result<
    (
        openclank_history::catalog::ResourceKey,
        ResourceHandle,
        ResourceRegistration,
    ),
    &'static str,
> {
    let registry=ResourceRegistry::load(resource_map).map_err(|_| "resource_unavailable")?;
    registered_files_resource_key_in(resource_id,auth,grants,&registry,host_root,authorized_roots,allow_inactive_history)
}

fn registered_files_resource_key_in(
    resource_id: &str,
    auth: &openclank_history::protocol::AuthContext,
    grants: &[(String, String)],
    registry: &ResourceRegistry,
    host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
    allow_inactive_history: bool,
) -> Result<
    (
        openclank_history::catalog::ResourceKey,
        ResourceHandle,
        ResourceRegistration,
    ),
    &'static str,
> {
    if resource_id.is_empty() || resource_id.len() > 256 {
        return Err("resource_not_found");
    }
    let Some(entry) = registry.entries.get(resource_id).cloned() else {
        return Err("resource_not_found");
    };
    if (!entry.active && !allow_inactive_history)
        || entry.account_id.is_empty()
        || entry.workspace_id.is_empty()
        || !owner_allowed(
            &entry.account_id,
            &auth.actor_id,
            &auth.account_id,
            grants,
        )
    {
        return Err("resource_unavailable");
    }
    if resolve_registered_path(&auth.actor_id, &entry, host_root, authorized_roots).is_err() {
        return Err("resource_unavailable");
    }
    let key = openclank_history::catalog::ResourceKey {
        account_id: entry.account_id.clone(),
        workspace_id: entry.workspace_id.clone(),
        provider: "odysseus-files".into(),
        resource_id: entry.resource_id.clone(),
    };
    let handle = ResourceHandle {
        resource_id: entry.resource_id.clone(),
        account_id: entry.account_id.clone(),
        workspace_id: entry.workspace_id.clone(),
        generation: entry.generation,
    };
    Ok((key, handle, entry))
}

fn checkpoint_action_request(
    envelope: &ControlEnvelope, request_id: &str, workspace_id: &str, root_id: &str,
    items: &[openclank_history::protocol::CheckpointSelection], binding: &ServerCredential,
    grants: &[(String, String)], resource_map: &std::path::Path, host_root: &std::path::Path,
    roots: &[AuthorizedHistoryRoot],
) -> Result<(openclank_history::operations::ActionRequest, u64, Vec<openclank_history::protocol::CheckpointBinding>), String> {
    if binding.actor_id == "*" || binding.actor_id != envelope.auth.actor_id
        || binding.account_id != envelope.auth.account_id || binding.checkpoint_root_id.as_deref() != Some(root_id)
        || binding.capabilities != BTreeSet::from(["checkpoint-capture".to_owned()])
        || request_id.len() < 16 || request_id.len() > 80
        || !request_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
        || workspace_id.is_empty() || workspace_id.len() > 256 || root_id.is_empty() || root_id.len() > 256
        || items.is_empty() || items.len() > 16 { return Err("checkpoint authority unavailable".into()); }
    let action_id = format!("checkpoint-{:x}", Sha256::digest(format!("{}\0{}\0{}",binding.account_id,binding.actor_id,request_id).as_bytes()));
    if envelope.action_id != action_id { return Err("checkpoint action identity changed".into()); }
    let mut keys = Vec::with_capacity(items.len()); let mut bindings = Vec::with_capacity(items.len()); let mut seen = BTreeSet::new(); let mut total = 0u64;
    for item in items {
        if !seen.insert(&item.resource_id) { return Err("duplicate checkpoint resource".into()); }
        let Some((_, size)) = item.expected_fingerprint.rsplit_once(':') else { return Err("invalid checkpoint fingerprint".into()); };
        let size: u64 = size.parse().map_err(|_| "invalid checkpoint fingerprint")?;
        if size > 16 * 1024 * 1024 || !staged_fingerprint_valid(&item.expected_fingerprint, size) { return Err("invalid checkpoint fingerprint".into()); }
        total = total.checked_add(size).ok_or("checkpoint size overflow")?;
        let (key, _, entry) = registered_files_resource_key(&item.resource_id,&envelope.auth,grants,resource_map,host_root,roots,false)
            .map_err(|_| "checkpoint resource unavailable")?;
        if key.account_id != binding.account_id || key.workspace_id != workspace_id || entry.root_id != root_id {
            return Err("checkpoint resource scope changed".into());
        }
        bindings.push(openclank_history::protocol::CheckpointBinding { resource_id: key.resource_id.clone(),
            root_id: entry.root_id, workspace_id: entry.workspace_id, registry_generation: entry.generation,
            relative_path: entry.relative_path, staged_upload_id: String::new() });
        keys.push(key);
    }
    if total > 64 * 1024 * 1024 { return Err("checkpoint total exceeds bound".into()); }
    Ok((openclank_history::operations::ActionRequest {
        schema_version: 1, action_id, actor_account_id: binding.account_id.clone(), resource_key: keys[0].clone(),
        physical_lease_keys: Vec::new(), guard_resource_ids: Vec::new(), modified_resource_ids: keys,
        operation: "selected_checkpoint_v1".into(), expected_revision: None, actor_id: binding.actor_id.clone(),
        actor_kind: "files".into(), managed_tool_origin_v1: None, session_id: None, run_id: None, task_id: None,
        tool_id: Some("odysseus-files".into()), before_revision: None, expected_after_revision: None,
        original_locator: None, destination_locator: None, timestamp_millis: None,
        coverage: Some(openclank_history::catalog::CaptureManifest { metadata: Some(serde_json::json!({
            "root_id": root_id, "workspace_id": workspace_id, "items": items, "bindings": bindings,
            "consistency": "observed_interval", "quiescence": "unverified" })), ..Default::default() }),
        per_resource_outcomes: None,
    }, total, bindings))
}

fn checkpoint_status(record: &ActionRecord, claimed: bool) -> Result<ServiceResponse, String> {
    use base64::Engine as _;
    if record.operation != "selected_checkpoint_v1" { return Err("checkpoint unavailable".into()); }
    let metadata = record.coverage.as_ref().and_then(|c|c.metadata.as_ref()).and_then(|v|v.as_object()).ok_or("checkpoint metadata unavailable")?;
    let state = match &record.state {
        ActionState::CheckpointPending if claimed => "claimed",
        ActionState::CheckpointPending | ActionState::CheckpointCapturing | ActionState::CheckpointFinalizing => "pending",
        ActionState::CheckpointFailed => "failed",
        ActionState::CheckpointComplete => "complete",
        _ => return Err("checkpoint state unavailable".into()),
    };
    let mut versions = Vec::new(); let mut locator = None;
    if state == "complete" {
        if record.live.is_some() || record.before_resources.len() != record.modified_resource_ids.len() || record.before_resources.is_empty() {
            return Err("checkpoint receipt unavailable".into());
        }
        for (entry, expected) in record.before_resources.iter().zip(&record.modified_resource_ids) {
            let receipt = entry.before.as_ref().ok_or("checkpoint version unavailable")?;
            if &entry.resource_key != expected { return Err("checkpoint resource changed".into()); }
            versions.push(openclank_history::protocol::CheckpointVersion {
                resource_id: expected.resource_id.clone(),
                version_ref: openclank_history::operations::checkpoint_version_ref(&record.action_id, &receipt.version_id),
                fingerprint: receipt.fingerprint.clone(), size: entry.coverage.byte_len.ok_or("checkpoint size unavailable")?,
            });
        }
        let value = serde_json::json!({"version":1,"account_id":record.actor_account_id,"workspace_id":record.resource_key.workspace_id,
            "root_id":metadata.get("root_id").and_then(|v|v.as_str()).ok_or("checkpoint root unavailable")?,
            "action_id":record.action_id,"request_digest":record.request_digest,"items":&versions});
        let raw = serde_json::to_vec(&value).map_err(|_| "checkpoint receipt unavailable")?;
        if raw.len() > 16384 { return Err("checkpoint receipt exceeds bound".into()); }
        locator = Some(format!("oc-history-checkpoint:v1:{}",base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw)));
    }
    Ok(ServiceResponse::CheckpointStatus { state: state.into(), request_digest: Some(record.request_digest.clone()),
        locator, items: versions, consistency: (state=="complete").then(||"observed_interval".into()),
        quiescence: (state=="complete").then(||"unverified".into()),
        started_millis: metadata.get("started_millis").and_then(|v|v.as_u64()),
        finished_millis: metadata.get("finished_millis").and_then(|v|v.as_u64()) })
}

async fn finalize_checkpoint_fast(
    envelope: ControlEnvelope, request_id: String, workspace_id: String, root_id: String,
    items: Vec<openclank_history::protocol::CheckpointSelection>, staged: Vec<openclank_history::protocol::CheckpointStaged>,
    started_millis: u64, finished_millis: u64, auth: &AuthBindings,
    coordinator: &std::sync::Arc<tokio::sync::Mutex<HistoryCoordinator>>,
    uploads: &std::sync::Arc<tokio::sync::Mutex<BTreeMap<String, StagedUpload>>>,
    staged_bytes: &std::sync::Arc<std::sync::atomic::AtomicU64>, authority_file: Option<&std::path::Path>,
    grants: &[(String,String)], resource_map: &std::path::Path, host_root: &std::path::Path, roots: &[AuthorizedHistoryRoot],
) -> ServiceResponse {
    let unavailable = || ServiceResponse::Unavailable {code:"checkpoint_unavailable".into(),message:"Checkpoint unavailable".into()};
    let result = async {
        let binding = auth.validate_checkpoint(&envelope,&root_id).map_err(|_| "checkpoint unavailable")?;
        let (request, total, _) = checkpoint_action_request(&envelope,&request_id,&workspace_id,&root_id,&items,&binding,grants,resource_map,host_root,roots)?;
        let now=registry_timestamp();
        if staged.len()!=items.len() || staged.is_empty() || started_millis==0 || finished_millis<started_millis
            || finished_millis-started_millis>30_000 || finished_millis>now || now-finished_millis>30_000 {return Err("checkpoint unavailable".into());}
        let digest = openclank_history::operations::request_digest(&request);
        {
            let owner = coordinator.lock().await;
            let record = owner.catalog().get_action(&envelope.action_id).map_err(|_| "checkpoint unavailable")?.ok_or("checkpoint unavailable")?;
            if record.request_digest != digest || record.state != ActionState::CheckpointPending || record.before_logical_bytes!=total
                || record.before_scope_id.is_none() {return Err("checkpoint unavailable".into());}
        }
        let mut owned = Vec::with_capacity(staged.len()); let mut seen = BTreeSet::new();
        {
            let table = uploads.lock().await;
            for (selection, stage) in items.iter().zip(&staged) {
                if selection.resource_id!=stage.resource_id || selection.expected_fingerprint!=stage.fingerprint
                    || !staged_fingerprint_valid(&stage.fingerprint,stage.size) || !seen.insert(&stage.staged_upload_id) {return Err("checkpoint unavailable".into());}
                let upload=table.get(&stage.staged_upload_id).ok_or("checkpoint unavailable")?;
                if upload.actor_id!=envelope.auth.actor_id || upload.account_id!=envelope.auth.account_id
                    || upload.action_id!=envelope.action_id || upload.content_length!=stage.size || upload.fingerprint!=stage.fingerprint || !upload.finished {
                    return Err("checkpoint unavailable".into());
                }
                owned.push(OwnedStagedUpload {upload_id:stage.staged_upload_id.clone(),io_lock:upload.io_lock.clone()});
            }
        }
        coordinator.lock().await.start_selected_checkpoint(&envelope.action_id,&digest,total).map_err(|_| "checkpoint unavailable")?;
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(30);
        let mut entries=Vec::with_capacity(staged.len());
        for ((key, stage), captured) in request.modified_resource_ids.iter().zip(&staged).zip(&owned) {
            if std::time::Instant::now()>=deadline {return Err("checkpoint unavailable".into());}
            let bytes=read_owned_staged_upload(uploads,captured,&envelope.auth.actor_id,&envelope.auth.account_id,&envelope.action_id,&stage.fingerprint).await?;
            entries.push(BatchCaptureInput {resource_key:key.clone(),old_locator:None,new_locator:None,
                expected_revision:Some(openclank_history::catalog::Revision::Opaque {kind:"fingerprint".into(),value:stage.fingerprint.clone()}),
                existence:openclank_history::catalog::ResourceExistence::Present,resource_type:openclank_history::catalog::ResourceType::File,
                metadata:openclank_history::catalog::ResourceMetadata {size:Some(stage.size),..Default::default()},content:Some(bytes),
                fingerprint:stage.fingerprint.clone(),coverage:openclank_history::catalog::CaptureManifest {byte_len:Some(stage.size),..Default::default()} });
        }
        if std::time::Instant::now()>=deadline {return Err("checkpoint unavailable".into());}
        // Scope can rotate while staging is read. Re-read the existing authority
        // manifest and exact registry before allowing the durable completion.
        let mut current = auth.clone(); let (_, current_roots)=current.reload_authority_file(authority_file);
        let current_binding=current.validate_checkpoint(&envelope,&root_id).map_err(|_| "checkpoint unavailable")?;
        let (current_request,_,_)=checkpoint_action_request(&envelope,&request_id,&workspace_id,&root_id,&items,&current_binding,&current.grants(&[]),resource_map,host_root,&current_roots)?;
        if openclank_history::operations::request_digest(&current_request)!=digest {return Err("checkpoint unavailable".into());}
        // Status/read uses this same coordinator gate: do not reveal terminal
        // completion while the original staged-upload cleanup is still live.
        let owner=coordinator.lock().await;
        owner.finalize_selected_checkpoint(&envelope.action_id,entries,started_millis,finished_millis).await.map_err(|_| "checkpoint unavailable")?;
        remove_checkpoint_stages_checked(uploads,staged_bytes,&envelope.auth.actor_id,&envelope.auth.account_id,&envelope.action_id,&owned).await?;
        let record=owner.finish_selected_checkpoint_cleanup(&envelope.action_id).map_err(|_| "checkpoint unavailable")?;
        drop(owner);
        checkpoint_status(&record,false)
    }.await;
    result.unwrap_or_else(|_|unavailable())
}

fn request_resources_allowed(
    request: &openclank_history::operations::ActionRequest,
    actor: &str,
    account: &str,
    grants: &[(String, String)],
) -> bool {
    owner_allowed(&request.resource_key.account_id, actor, account, grants)
        && request
            .guard_resource_ids
            .iter()
            .chain(request.modified_resource_ids.iter())
            .all(|resource| owner_allowed(&resource.account_id, actor, account, grants))
}

fn trusted_registry_aliases(
    request: &openclank_history::operations::ActionRequest,
    resource_map: &std::path::Path,
) -> Vec<String> {
    let Ok(registry) = ResourceRegistry::load(resource_map) else {
        return Vec::new();
    };
    request
        .guard_resource_ids
        .iter()
        .chain(request.modified_resource_ids.iter())
        .filter_map(|resource| {
            registry.entries.get(&resource.resource_id).filter(|entry| {
                entry.active
                    && entry.account_id == resource.account_id
                    && entry.workspace_id == resource.workspace_id
            })
        })
        .map(|entry| {
            format!(
                "registry:{}:{}:{}:{}",
                entry.account_id, entry.workspace_id, entry.root_id, entry.relative_path
            )
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn complete_batch_digest(entries: &[BatchCompleteEntry]) -> String {
    let bytes = serde_json::to_vec(entries).expect("complete batch entries are serializable");
    blake3::hash(&bytes).to_hex().to_string()
}

fn authorize(
    coordinator: &HistoryCoordinator,
    envelope: &ControlEnvelope,
    account: &str,
    grants: &[(String, String)],
) -> Result<ActionRecord, String> {
    let action = coordinator
        .catalog()
        .get_action(&envelope.action_id)
        .map_err(|error| error.to_string())?
        .ok_or_else(|| "unknown action".to_owned())?;
    if action.operation == "selected_checkpoint_v1" { return Err("checkpoint requires its dedicated status path".into()); }
    if action.actor_account_id != account
        || action.actor_id != envelope.auth.actor_id
        || !owner_allowed(
            &action.resource_key.account_id,
            &envelope.auth.actor_id,
            account,
            grants,
        )
        || !action
            .guard_resource_ids
            .iter()
            .chain(action.modified_resource_ids.iter())
            .all(|resource| {
                owner_allowed(
                    &resource.account_id,
                    &envelope.auth.actor_id,
                    account,
                    grants,
                )
            })
    {
        return Err("actor/account/owner binding mismatch".into());
    }
    Ok(action)
}

fn valid_resource_id(resource_id: &str) -> bool {
    resource_id.starts_with("file:")
        && resource_id.len() <= 256
        && !resource_id
            .chars()
            .any(|character| character.is_whitespace() || matches!(character, '/' | '\\' | '\0'))
}

/// A restricted registered resource cannot be borrowed by another actor in
/// capture/batch/control requests, even when the account credential is wildcard.
fn restricted_resource_frame(frame: &[u8], map: &std::path::Path, roots: &[AuthorizedHistoryRoot], bindings: &AuthBindings, coordinator: &HistoryCoordinator) -> bool {
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(frame) else { return false; };
    let Some(body) = value.as_object().and_then(|object| object.values().next()) else { return false; };
    let Some(auth_value) = body.get("envelope").unwrap_or(body).get("auth") else { return false; };
    let Ok(auth) = serde_json::from_value::<openclank_history::protocol::AuthContext>(auth_value.clone()) else { return false; };
    let exact_binding = bindings.authenticate(&auth).is_ok_and(|binding| binding.actor_id != "*" && binding.actor_id == auth.actor_id);
    let Ok(registry) = ResourceRegistry::load(map) else { return roots.iter().any(|root| !root.actor_ids.is_empty()); };
    fn root_denied(actors: &BTreeSet<String>, actor: &str, exact: bool) -> bool { !actors.is_empty() && (!exact || !actors.contains(actor)) }
    fn entry_denied(entry: &ResourceRegistration, actor: &str, exact: bool, roots: &[AuthorizedHistoryRoot]) -> bool {
        root_denied(&entry.actor_ids, actor, exact) || roots.iter().any(|root| root.root_id == entry.root_id && root_denied(&root.actor_ids, actor, exact))
    }
    fn denied(value: &serde_json::Value, actor: &str, exact: bool, registry: &ResourceRegistry, roots: &[AuthorizedHistoryRoot], coordinator: &HistoryCoordinator) -> bool {
        match value {
            serde_json::Value::Object(object) => {
                if let Some(entry) = object.get("resource_id").and_then(|id| id.as_str()).and_then(|id| registry.entries.get(id)) {
                    if entry_denied(entry, actor, exact, roots) { return true; }
                }
                if let Some(root) = object.get("root_id").and_then(|id| id.as_str()).and_then(|id| roots.iter().find(|root| root.root_id == id)) {
                    if root_denied(&root.actor_ids, actor, exact) { return true; }
                }
                for key in ["action_id", "source_action_id"] {
                    if let Some(action) = object.get(key).and_then(|id| id.as_str()).and_then(|id| coordinator.catalog().get_action(id).ok().flatten()) {
                        for resource in std::iter::once(&action.resource_key).chain(action.guard_resource_ids.iter()).chain(action.modified_resource_ids.iter()) {
                            if registry.entries.get(&resource.resource_id).is_some_and(|entry| entry_denied(entry, actor, exact, roots)) { return true; }
                        }
                    }
                }
                object.values().any(|value| denied(value, actor, exact, registry, roots, coordinator))
            }
            serde_json::Value::Array(values) => values.iter().any(|value| denied(value, actor, exact, registry, roots, coordinator)),
            _ => false,
        }
    }
    denied(body, &auth.actor_id, exact_binding, &registry, roots, coordinator)
}

fn registry_timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

/// Called while the shared History coordinator lock is held. The action's
/// prepared metadata binds the complete ordered move plan, not just its IDs.
fn apply_registry_batch(
    registry: &mut ResourceRegistry, action: &ActionRecord, actor_id: &str,
    account_id: &str, workspace_id: &str, moves: &[RegistryBatchMove], subtree_guard: Option<&RegistrySubtreeGuard>, validate_only: bool,
    host_root: &std::path::Path, authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<bool, String> {
    if moves.is_empty() || moves.len() > MAX_BATCH_RESOURCES || action.operation != "managed_move"
        || if validate_only { subtree_guard.is_none() || !matches!(&action.state, ActionState::BeforeDurable) }
           else { !matches!(&action.state, ActionState::Applied | ActionState::Complete) } {
        return Err("registry batch action or size is invalid".into());
    }
    let value = match subtree_guard {
        Some(guard) => serde_json::json!({"moves":moves,"subtree_guard":guard}),
        None => serde_json::to_value(moves).map_err(|_| "registry plan is invalid")?,
    };
    let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&value).map_err(|_| "registry plan is invalid")?));
    let expected_digest = action.coverage.as_ref().and_then(|coverage| coverage.metadata.as_ref())
        .and_then(|metadata| metadata.get("managed_registry_plan_digest")).and_then(serde_json::Value::as_str);
    if expected_digest != Some(digest.as_str()) { return Err("registry plan differs from captured action".into()); }
    let action_ids: BTreeSet<&str> = action.modified_resource_ids.iter()
        .filter(|key| key.account_id == account_id && key.workspace_id == workspace_id && key.provider == "odysseus-files")
        .map(|key| key.resource_id.as_str()).collect();
    let binding_allowed = |entry: &ResourceRegistration| -> bool {
        (entry.actor_ids.is_empty() || entry.actor_ids.contains(actor_id))
            && ResourceRegistry::validate_relative_path(&entry.relative_path)
            && registry_destination(actor_id, account_id, workspace_id, Some(&entry.root_id), &entry.root_path, ".", host_root, authorized_roots).is_ok()
    };
    let mut ids = BTreeSet::new(); let mut destinations = BTreeSet::new(); let mut resolved = Vec::new();
    for item in moves {
        if !ids.insert(item.resource_id.clone()) || !action_ids.contains(item.resource_id.as_str())
            || item.expected_generation == u64::MAX || item.replaced_resource_id.is_some() != item.replaced_generation.is_some() {
            return Err("registry batch participant is invalid".into());
        }
        if let Some(id) = &item.replaced_resource_id {
            if !ids.insert(id.clone()) || !action_ids.contains(id.as_str()) || item.replaced_generation == Some(u64::MAX) {
                return Err("registry batch replacement is invalid".into());
            }
        }
        let (root_id, root_path) = match registry_destination(actor_id, account_id, workspace_id,
            Some(&item.destination_root_id), &item.destination_root_path, &item.destination_relative_path,
            host_root, authorized_roots) {
            Ok(value) => value,
            Err(error) if validate_only && subtree_guard.is_some() => {
                let placeholder = item.replaced_resource_id.as_ref().and_then(|id| registry.entries.get(id))
                    .filter(|entry| entry.resource_id.starts_with("file:folder:") && entry.active
                        && action_ids.contains(entry.resource_id.as_str()) && entry.account_id == account_id
                        && entry.workspace_id == workspace_id && entry.root_id == item.destination_root_id
                        && entry.root_path == item.destination_root_path && entry.relative_path == item.destination_relative_path
                        && Some(entry.generation) == item.replaced_generation && binding_allowed(entry));
                if placeholder.is_none() { return Err(error); }
                registry_destination(actor_id, account_id, workspace_id, Some(&item.destination_root_id),
                    &item.destination_root_path, ".", host_root, authorized_roots)?
            }
            Err(error) => return Err(error),
        };
        if !destinations.insert((root_id.clone(), root_path.clone(), item.destination_relative_path.clone())) {
            return Err("registry batch destination is duplicated".into());
        }
        let source = registry.entries.get(&item.resource_id).ok_or("registry batch source is unavailable")?;
        if !source.active || source.account_id != account_id || source.workspace_id != workspace_id
            || !binding_allowed(source) {
            return Err("registry batch source is unauthorized".into());
        }
        let replacement = if let Some(id) = &item.replaced_resource_id {
            let entry = registry.entries.get(id).ok_or("registry batch replacement is unavailable")?;
            if entry.account_id != account_id || entry.workspace_id != workspace_id
                || !binding_allowed(entry) {
                return Err("registry batch replacement is unauthorized".into());
            }
            Some(entry)
        } else { None };
        let before = source.generation == item.expected_generation && source.root_id == item.expected_root_id
            && source.relative_path == item.expected_relative_path
            && replacement.map_or(true, |entry| entry.active && entry.generation == item.replaced_generation.unwrap()
                && entry.root_path == root_path && entry.relative_path == item.destination_relative_path)
            && registry.active_for_path(account_id, workspace_id, &root_path, &item.destination_relative_path)
                .map_or(item.replaced_resource_id.is_none(), |entry| item.replaced_resource_id.as_deref() == Some(entry.resource_id.as_str()));
        let after = source.generation == item.expected_generation + 1 && source.root_id == root_id
            && source.root_path == root_path && source.relative_path == item.destination_relative_path
            && registry.active_for_path(account_id, workspace_id, &root_path, &item.destination_relative_path)
                .is_some_and(|entry| entry.resource_id == item.resource_id)
            && replacement.map_or(true, |entry| !entry.active && entry.generation == item.replaced_generation.unwrap() + 1
                && entry.root_path == root_path && entry.relative_path == item.destination_relative_path);
        resolved.push((root_id, root_path, before, after));
    }
    let mut subtree_before = true;
    let mut subtree_after = true;
    if let Some(guard) = subtree_guard {
        use std::path::Path;
        let source = Path::new(&guard.source_relative_path);
        let destination = Path::new(&guard.destination_relative_path);
        if !ResourceRegistry::validate_relative_path(&guard.source_relative_path)
            || !ResourceRegistry::validate_relative_path(&guard.destination_relative_path)
            || guard.destination_root_id.is_some() != guard.destination_root_path.is_some() {
            return Err("registry subtree is invalid".into());
        }
        let (root_id, root_path) = registry_destination(actor_id, account_id, workspace_id,
            Some(&guard.root_id), &guard.root_path, ".", host_root, authorized_roots)?;
        let (destination_root_id, destination_root_path) = match (&guard.destination_root_id, &guard.destination_root_path) {
            (Some(id), Some(path)) => registry_destination(actor_id, account_id, workspace_id,
                Some(id), path, ".", host_root, authorized_roots)?,
            (None, None) => (root_id.clone(), root_path.clone()),
            _ => return Err("registry subtree is invalid".into()),
        };
        let cross=guard.destination_root_id.is_some();
        if guard.source_root_identity.is_some() != cross || guard.destination_root_identity.is_some() != cross {return Err("registry root identity is unavailable".into());}
        if cross {
            #[cfg(unix)] {
                use std::os::unix::fs::MetadataExt;
                let identity=|path:&str| -> Result<serde_json::Value,String> {
                    let metadata=std::fs::symlink_metadata(path).map_err(|_| "registry root unavailable")?;
                    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {return Err("registry root unavailable".into());}
                    Ok(serde_json::json!({"kind":"unix","device":metadata.dev(),"inode":metadata.ino()}))
                };
                let source_identity=identity(&root_path)?;let destination_identity=identity(&destination_root_path)?;
                if guard.source_root_identity.as_ref()!=Some(&source_identity) || guard.destination_root_identity.as_ref()!=Some(&destination_identity)
                    || source_identity.get("device")!=destination_identity.get("device") {return Err("registry roots changed".into());}
            }
            #[cfg(not(unix))] {return Err("registry cross-root unavailable".into());}
        }
        let source_absolute = Path::new(&root_path).join(source);
        let destination_absolute = Path::new(&destination_root_path).join(destination);
        if source_absolute.starts_with(&destination_absolute) || destination_absolute.starts_with(&source_absolute) {
            return Err("registry subtree is invalid".into());
        }
        let mut captured_paths = BTreeSet::new();
        for item in moves {
            if item.expected_root_id == root_id { captured_paths.insert(Path::new(&root_path).join(&item.expected_relative_path)); }
            if item.destination_root_id == destination_root_id && item.destination_root_path == destination_root_path {
                captured_paths.insert(Path::new(&destination_root_path).join(&item.destination_relative_path));
            }
        }
        for id in &action_ids {
            if let Some(entry) = registry.entries.get(*id) {
                captured_paths.insert(Path::new(&entry.root_path).join(&entry.relative_path));
            }
        }
        for entry in registry.entries.values().filter(|entry| entry.active) {
            if !ResourceRegistry::validate_relative_path(&entry.relative_path) { continue; }
            let absolute = Path::new(&entry.root_path).join(&entry.relative_path);
            let in_source=absolute.starts_with(&source_absolute);
            let in_destination=absolute.starts_with(&destination_absolute);
            let known_root=(entry.root_id == root_id && entry.root_path == root_path)
                || (entry.root_id == destination_root_id && entry.root_path == destination_root_path);
            if (in_source || in_destination || captured_paths.contains(&absolute))
                && (entry.account_id != account_id || entry.workspace_id != workspace_id || !known_root
                    || (in_source && (entry.root_id != root_id || entry.root_path != root_path))
                    || (in_destination && (entry.root_id != destination_root_id || entry.root_path != destination_root_path))) {
                return Err("registry batch changed; review required".into());
            }
        }
        let mut expected_source = BTreeSet::new();
        let mut expected_destination_before = BTreeSet::new();
        let mut expected_destination_after = BTreeSet::new();
        for item in moves {
            let from = Path::new(&item.expected_relative_path);
            let in_source=item.expected_root_id == root_id && from.starts_with(source);
            let target=Path::new(&item.destination_relative_path);
            let in_destination=item.destination_root_id == destination_root_id && item.destination_root_path == destination_root_path && target.starts_with(destination);
            if !in_source && !(cross && in_destination) {continue;}
            if in_source && (item.destination_root_id != destination_root_id || item.destination_root_path != destination_root_path) {
                return Err("registry subtree move differs from its root".into());
            }
            let structural=in_source && target == destination.join(from.strip_prefix(source).map_err(|_| "registry subtree invalid")?);
            if !structural {
                // A nonstructural target must be this action's independently
                // proof-registered asset placeholder, not an arbitrary entry.
                if !cross {return Err("registry subtree move differs from its root".into());}
                let action_hash=action.action_id.strip_prefix("odysseus-managed-").ok_or("registry action identity invalid")?;
                let mapping=format!("{}\0{}\0{}\0{}\0{}\0{}",root_id,root_path,source.display(),destination_root_id,destination_root_path,destination.display());
                let expected=format!("file:folder:{}:{:x}:{:x}",action_hash,Sha256::digest(mapping.as_bytes()),Sha256::digest(item.resource_id.as_bytes()));
                if item.replaced_resource_id.as_deref()!=Some(expected.as_str()) {return Err("registry folder asset proof unavailable".into());}
            }
            if in_source {expected_source.insert((item.resource_id.clone(), item.expected_generation, item.expected_relative_path.clone()));}
            if in_destination {
                if let (Some(id), Some(generation)) = (&item.replaced_resource_id, item.replaced_generation) {
                    expected_destination_before.insert((id.clone(), generation, item.destination_relative_path.clone()));
                }
                expected_destination_after.insert((item.resource_id.clone(), item.expected_generation + 1, item.destination_relative_path.clone()));
            }
        }
        if expected_source.is_empty() { return Err("registry subtree is empty".into()); }
        let mut actual_source = BTreeSet::new();
        let mut actual_destination = BTreeSet::new();
        for entry in registry.entries.values().filter(|entry| entry.active && entry.account_id == account_id && entry.workspace_id == workspace_id) {
            let path = Path::new(&entry.relative_path);
            let in_source=entry.root_id == root_id && entry.root_path == root_path && path.starts_with(source);
            let in_destination=entry.root_id == destination_root_id && entry.root_path == destination_root_path && path.starts_with(destination);
            if in_source || in_destination {
                if !binding_allowed(entry) { return Err("registry subtree is unauthorized".into()); }
                let identity = (entry.resource_id.clone(), entry.generation, entry.relative_path.clone());
                if in_source { actual_source.insert(identity); }
                else { actual_destination.insert(identity); }
            }
        }
        subtree_before = actual_source == expected_source && actual_destination == expected_destination_before;
        subtree_after = actual_source.is_empty() && actual_destination == expected_destination_after;
    }
    if validate_only {
        return if subtree_before && resolved.iter().all(|item| item.2) { Ok(false) }
            else { Err("registry batch changed; review required".into()) };
    }
    if subtree_after && resolved.iter().all(|item| item.3) { return Ok(false); }
    if !subtree_before || !resolved.iter().all(|item| item.2) { return Err("registry batch changed; review required".into()); }
    for (item, (root_id, root_path, _, _)) in moves.iter().zip(resolved) {
        if let Some(id) = &item.replaced_resource_id {
            let entry = registry.entries.get_mut(id).ok_or("registry batch replacement disappeared")?;
            entry.active = false; entry.generation += 1; entry.updated_millis = registry_timestamp();
        }
        let entry = registry.entries.get_mut(&item.resource_id).ok_or("registry batch source disappeared")?;
        entry.actor_ids = authorized_roots.iter().find(|root| root.root_id == root_id).map(|root| root.actor_ids.clone()).unwrap_or_default();
        entry.root_id = root_id; entry.root_path = root_path; entry.relative_path = item.destination_relative_path.clone();
        entry.generation += 1; entry.updated_millis = registry_timestamp();
    }
    Ok(true)
}

/// Registration-only lookup under an existing authorized recursive root.
/// The action need not exist yet; its identity is bound by the caller arm.
#[cfg(target_os="linux")]
fn registry_checkout_absent(actor_id:&str,account_id:&str,workspace_id:&str,root_id:&str,root_path:&str,relative_path:&str,paths:&[String],directories:&[String],host_root:&std::path::Path,roots:&[AuthorizedHistoryRoot])->Result<(String,String),String>{
    use std::ffi::CString;use std::os::fd::{AsRawFd,FromRawFd};use std::os::unix::ffi::OsStrExt;use std::os::unix::fs::OpenOptionsExt;use std::path::{Component,Path};
    if roots.is_empty()||paths.is_empty()||paths.len()>64||directories.len()>paths.len()||!paths.iter().any(|p|p==relative_path)||paths.iter().collect::<std::collections::BTreeSet<_>>().len()!=paths.len()||directories.iter().collect::<std::collections::BTreeSet<_>>().len()!=directories.len() {return Err("checkout paths unavailable".into())}
    for item in paths.iter().chain(directories) {if !ResourceRegistry::validate_relative_path(item)||item.split('/').any(|x|x.is_empty()||x=="."||x==".."||matches!(x.to_ascii_lowercase().as_str(),".env"|".secrets"|"auth"|"credentials"|"tokens"|"settings"))||item=="." {return Err("checkout path invalid".into())}}
    if directories.iter().any(|v|!paths.contains(v)){return Err("checkout directories invalid".into())}
    let binding=roots.iter().find(|r|r.root_id==root_id).ok_or("checkout root unavailable")?;if root_is_exact(binding){return Err("checkout root unavailable".into())}restricted_child(binding,relative_path)?;
    let (id,root)=registry_destination(actor_id,account_id,workspace_id,Some(root_id),root_path,".",host_root,roots)?;
    let components:Vec<_>=Path::new(relative_path).components().collect();let mut fd=std::fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&root).map_err(|e|e.to_string())?;let mut suffix=std::path::PathBuf::new();let mut missing=false;
    for (i,part) in components.iter().enumerate(){let Component::Normal(name)=part else{return Err("checkout path invalid".into())};suffix.push(name);
        if missing {if i+1<components.len()&&!directories.iter().any(|d|Path::new(d)==suffix){return Err("checkout missing ancestor not frozen".into())}continue}
        let n=CString::new(name.as_bytes()).map_err(|_|"checkout path invalid")?;let mut st:libc::stat=unsafe{std::mem::zeroed()};
        if unsafe{libc::fstatat(fd.as_raw_fd(),n.as_ptr(),&mut st,libc::AT_SYMLINK_NOFOLLOW)}!=0 {let e=std::io::Error::last_os_error();if e.kind()!=std::io::ErrorKind::NotFound{return Err("checkout ancestor unavailable".into())}if i+1<components.len()&&!directories.iter().any(|d|Path::new(d)==suffix){return Err("checkout missing ancestor not frozen".into())}missing=true;continue}
        if i+1==components.len(){return Err("checkout destination no longer absent".into())}if st.st_mode&libc::S_IFMT!=libc::S_IFDIR{return Err("checkout ancestor invalid".into())}
        let child=unsafe{libc::openat(fd.as_raw_fd(),n.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};if child<0{return Err("checkout ancestor changed".into())}fd=unsafe{std::fs::File::from_raw_fd(child)};
    }
    if !missing{return Err("checkout destination is present".into())}Ok((id,root))
}
#[cfg(not(target_os="linux"))]
fn registry_checkout_absent(_actor:&str,_account:&str,_workspace:&str,_id:&str,_path:&str,_relative:&str,_paths:&[String],_directories:&[String],_host:&std::path::Path,_roots:&[AuthorizedHistoryRoot])->Result<(String,String),String>{Err("checkout registration unavailable".into())}

fn registry_registration(
    registry: &ResourceRegistry,
    actor_id: &str,
    resource_id: &str,
    account_id: &str,
    workspace_id: &str,
    root_id: Option<&str>,
    root_path: &str,
    relative_path: &str,
    host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
    managed_pending_media: bool,
    checkout_root: Option<(String,String)>,
) -> Result<(ResourceRegistration, bool), String> {
    if account_id.is_empty() || workspace_id.is_empty() {
        return Err("resource registration identity is incomplete".into());
    }
    if !ResourceRegistry::validate_relative_path(relative_path) {
        return Err("resource registration path is invalid".into());
    }
    let checkout=checkout_root.is_some();
    let (root_id, root_path) = if let Some(root)=checkout_root {root} else if managed_pending_media { registry_managed_media_destination(
        actor_id, account_id, workspace_id, root_id, root_path, relative_path, host_root, authorized_roots,
    )? } else { registry_destination(
        actor_id,
        account_id,
        workspace_id,
        root_id,
        root_path,
        relative_path,
        host_root,
        authorized_roots,
    )? };
    if !resource_id.is_empty() && !valid_resource_id(resource_id) {
        return Err("resource registration id is not opaque".into());
    }
    if let Some(existing) =
        registry.active_for_path(account_id, workspace_id, &root_path, relative_path)
    {
        if !managed_pending_media && !checkout {
            resolve_registered_path(actor_id, existing, host_root, authorized_roots)?;
        }
        if !resource_id.is_empty() && existing.resource_id != resource_id {
            return Err("resource path is already bound to another identity".into());
        }
        return Ok((existing.clone(), false));
    }
    let id = if resource_id.is_empty() {
        registry.mint_id()
    } else {
        resource_id.to_owned()
    };
    if let Some(existing) = registry.entries.get(&id) {
        return Err(if existing.active {
            "resource id is already bound to another path".into()
        } else {
            "resource id is revoked and cannot be reused".into()
        });
    }
    Ok((
        ResourceRegistration {
            actor_ids: authorized_roots.iter().find(|root| root.root_id == root_id).map(|root| root.actor_ids.clone()).unwrap_or_default(),
            resource_id: id,
            account_id: account_id.to_owned(),
            workspace_id: workspace_id.to_owned(),
            root_id,
            root_path,
            relative_path: relative_path.to_owned(),
            active: true,
            generation: 1,
            updated_millis: registry_timestamp(),
        },
        true,
    ))
}

/// Register only the exact missing counterpart of an existing source in a
/// frozen folder mapping. The deterministic ID fences changed same-op replay.
#[cfg(target_os = "linux")]
fn read_folder_proof_file(root: &std::path::Path, relative: &std::path::Path) -> Result<Vec<u8>, String> {
    use std::ffi::CString;
    use std::io::Read;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::path::Component;
    let parts:Vec<_> = relative.components().collect();
    if parts.is_empty() || parts.iter().any(|part| !matches!(part, Component::Normal(_))) {
        return Err("managed folder proof path is invalid".into());
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
    let mut parent = options.open(root).map_err(|_| "managed folder proof root is unavailable")?;
    for (index, part) in parts.iter().enumerate() {
        let Component::Normal(name) = part else { return Err("managed folder proof path is invalid".into()); };
        let name = CString::new(name.as_bytes()).map_err(|_| "managed folder proof path is invalid")?;
        let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC
            | if index + 1 == parts.len() { 0 } else { libc::O_DIRECTORY };
        // SAFETY: the parent descriptor and NUL-terminated name are live.
        let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 { return Err("managed folder proof file is unavailable".into()); }
        // SAFETY: openat returned a fresh owned descriptor.
        let mut opened = unsafe { std::fs::File::from_raw_fd(fd) };
        if index + 1 == parts.len() {
            let before = opened.metadata().map_err(|_| "managed folder proof file is unavailable")?;
            if !before.is_file() || before.len() > 10 * 1024 * 1024 { return Err("managed folder proof file is unavailable".into()); }
            let mut bytes = Vec::new();
            (&mut opened).take(10 * 1024 * 1024 + 1).read_to_end(&mut bytes).map_err(|_| "managed folder proof file is unavailable")?;
            let after = opened.metadata().map_err(|_| "managed folder proof file is unavailable")?;
            if before.dev() != after.dev() || before.ino() != after.ino() || before.len() != after.len()
                || before.mtime() != after.mtime() || before.mtime_nsec() != after.mtime_nsec()
                || bytes.len() as u64 != after.len() { return Err("managed folder proof file changed".into()); }
            return Ok(bytes);
        }
        parent = opened;
    }
    Err("managed folder proof file is unavailable".into())
}

#[cfg(not(target_os = "linux"))]
fn read_folder_proof_file(_root: &std::path::Path, _relative: &std::path::Path) -> Result<Vec<u8>, String> {
    Err("managed folder proof is unavailable on this platform".into())
}

fn compatible_folder_roots(source:&str,destination:&str)->Result<(),String> {
    if source==destination {return Ok(());}
    #[cfg(unix)] {
        use std::os::unix::fs::MetadataExt;
        let left=std::fs::symlink_metadata(source).map_err(|_|"folder root unavailable")?;
        let right=std::fs::symlink_metadata(destination).map_err(|_|"folder root unavailable")?;
        if !left.file_type().is_dir() || !right.file_type().is_dir() || left.file_type().is_symlink() || right.file_type().is_symlink() || left.dev()!=right.dev() {return Err("folder roots unavailable".into());}
        Ok(())
    }
    #[cfg(not(unix))] {Err("cross-root folders unavailable".into())}
}

fn registry_folder_destination(
    registry: &ResourceRegistry, actor_id: &str, account_id: &str, workspace_id: &str,
    root_id: &str, root_path: &str, operation_id: &str, source_resource_id: &str,
    source_folder: &str, destination_folder: &str, asset_proof: Option<&serde_json::Value>,
    destination_root_id: Option<&str>, destination_root_path: Option<&str>, host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<(ResourceRegistration, bool), String> {
    use std::path::{Component, Path};
    if operation_id.len() < 16 || operation_id.len() > 80
        || !operation_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_')
        || source_folder.contains('\\') || destination_folder.contains('\\')
        || !ResourceRegistry::validate_relative_path(source_folder) || !ResourceRegistry::validate_relative_path(destination_folder) {
        return Err("managed folder registration is invalid".into());
    }
    let source_folder = Path::new(source_folder); let destination_folder = Path::new(destination_folder);
    if source_folder == Path::new(".") || destination_folder == Path::new(".")
        || destination_root_id.is_some() != destination_root_path.is_some() {
        return Err("managed folder registration overlaps".into());
    }
    let protected = |path: &Path| path.components().any(|part| match part {
        Component::Normal(name) => matches!(name.to_string_lossy().to_ascii_lowercase().as_str(),
            ".env" | ".secrets" | "auth" | "credentials" | "tokens" | "settings"),
        _ => true,
    });
    if protected(source_folder) || protected(destination_folder) { return Err("managed folder registration is unavailable".into()); }
    let (root_id, canonical_root) = registry_destination(actor_id, account_id, workspace_id,
        Some(root_id), root_path, ".", host_root, authorized_roots)?;
    let (dest_id,dest_root)=match (destination_root_id,destination_root_path) {
        (Some(id),Some(path)) => registry_destination(actor_id,account_id,workspace_id,Some(id),path,".",host_root,authorized_roots)?,
        (None,None) => (root_id.clone(),canonical_root.clone()),
        _ => return Err("managed folder registration is invalid".into()),
    };
    compatible_folder_roots(&canonical_root,&dest_root)?;
    let source_absolute=Path::new(&canonical_root).join(source_folder);
    let destination_absolute=Path::new(&dest_root).join(destination_folder);
    if source_absolute.starts_with(&destination_absolute) || destination_absolute.starts_with(&source_absolute) {return Err("managed folder registration overlaps".into());}
    let source = registry.entries.get(source_resource_id).ok_or("managed folder source is unavailable")?;
    if !source.active || source.account_id != account_id || source.workspace_id != workspace_id
        || source.root_id != root_id || source.root_path != canonical_root {
        return Err("managed folder source is unavailable".into());
    }
    let source_path = Path::new(&source.relative_path);
    if source.relative_path.contains('\\') || protected(source_path) { return Err("managed folder source is unsupported".into()); }
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct FolderFrame { origin:String, canonical_root:std::path::PathBuf, layout_version:u8,
        workspace_id:Option<String>, workspace_relative_folder:String }
    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct AssetProof { path:std::path::PathBuf, destination:std::path::PathBuf, fingerprint:String,
        document:std::path::PathBuf, document_destination:std::path::PathBuf, workspace_root:std::path::PathBuf,
        origin:String, layout_version:u8, #[serde(default)] source_frame:Option<FolderFrame>, #[serde(default)] destination_frame:Option<FolderFrame> }
    let target = if let Some(value) = asset_proof {
        let proof:AssetProof = serde_json::from_value(value.clone()).map_err(|_| "managed folder asset proof is invalid")?;
        let root = Path::new(&canonical_root);
        if dest_id != root_id || dest_root != canonical_root {
            let frame=proof.destination_frame.as_ref().ok_or("cross-root folder asset proof unavailable")?;
            let source_frame=proof.source_frame.as_ref().ok_or("cross-root folder asset proof unavailable")?;
            if source_frame.origin != proof.origin || source_frame.canonical_root != proof.workspace_root || source_frame.layout_version != proof.layout_version
                || !source_frame.workspace_relative_folder.is_empty()
                || (source_frame.origin == "workspace" && source_frame.workspace_id.as_deref().is_none_or(str::is_empty))
                || (source_frame.origin != "workspace" && source_frame.workspace_id.is_some()) {return Err("folder source frame unavailable".into());}
            let destination_root=Path::new(&dest_root);
            let doc_suffix=proof.document.strip_prefix(root.join(source_folder)).map_err(|_| "managed folder document is unavailable")?;
            if doc_suffix.as_os_str().is_empty() || proof.document_destination != destination_root.join(destination_folder).join(doc_suffix)
                || proof.path != root.join(source_path) || !proof.workspace_root.starts_with(root)
                || !frame.canonical_root.starts_with(destination_root) || !proof.document_destination.starts_with(&frame.canonical_root)
                || !frame.workspace_relative_folder.is_empty() || proof.path.file_name() != proof.destination.file_name()
                || proof.fingerprint.is_empty() || protected(proof.document.strip_prefix(root).map_err(|_| "invalid document")?)
                || protected(proof.path.strip_prefix(root).map_err(|_| "invalid asset")?) {return Err("cross-root folder asset proof invalid".into());}
            let layout = |origin:&str, version:u8, base:&Path, document:&Path, filename:&std::ffi::OsStr| -> Result<std::path::PathBuf,String> {
                let relative=document.strip_prefix(base).map_err(|_| "folder document is outside frame")?;
                let stem=relative.file_stem().filter(|v| !v.is_empty()).ok_or("folder document stem unavailable")?;
                let parent=relative.parent().ok_or("folder document parent unavailable")?;
                Ok(match (origin,version) {
                    ("workspace",1) => base.join("media").join(parent).join(stem).join(filename),
                    ("workspace",2) | ("selected_root",2) => base.join(".media").join(parent).join(stem).join(filename),
                    ("loose",1) if parent.as_os_str().is_empty() => base.join("media").join(stem).join(filename),
                    _ => return Err("folder asset layout unsupported".into()),
                })
            };
            if (frame.origin == "workspace" && frame.workspace_id.as_deref().is_none_or(str::is_empty))
                || (frame.origin != "workspace" && frame.workspace_id.is_some())
                || (frame.origin == "workspace" && source_frame.origin == "workspace" && frame.workspace_id != source_frame.workspace_id) {
                return Err("folder frame unavailable".into());
            }
            let filename=proof.path.file_name().ok_or("folder asset name unavailable")?;
            let old=layout(&proof.origin,proof.layout_version,&proof.workspace_root,&proof.document,filename)?;
            let expected=layout(&frame.origin,frame.layout_version,&frame.canonical_root,&proof.document_destination,filename)?;
            if old != proof.path || expected != proof.destination || !expected.starts_with(destination_root)
                || protected(expected.strip_prefix(destination_root).map_err(|_| "invalid destination")?) {return Err("cross-root folder asset layout changed".into());}
            let _document=read_folder_proof_file(root,proof.document.strip_prefix(root).map_err(|_| "invalid document")?)?;
            let bytes=read_folder_proof_file(root,proof.path.strip_prefix(root).map_err(|_| "invalid asset")?)?;
            if format!("sha256:{:x}:{}",Sha256::digest(&bytes),bytes.len()) != proof.fingerprint {return Err("managed folder asset changed".into());}
            expected.strip_prefix(destination_root).map_err(|_| "invalid destination")?.to_path_buf()
        } else {
        let absolute_source = root.join(source_path);
        let document_suffix = proof.document.strip_prefix(root.join(source_folder)).map_err(|_| "managed folder document is unavailable")?;
        if document_suffix.as_os_str().is_empty() || proof.document_destination != root.join(destination_folder).join(document_suffix)
            || proof.path != absolute_source || proof.origin != "workspace" || !proof.workspace_root.starts_with(root)
            || !proof.document.starts_with(&proof.workspace_root) || proof.path.file_name() != proof.destination.file_name()
            || proof.fingerprint.is_empty() { return Err("managed folder asset proof is invalid".into()); }
        let doc_relative = proof.document_destination.strip_prefix(&proof.workspace_root).map_err(|_| "managed folder asset proof is invalid")?;
        let filename = proof.path.file_name().ok_or("managed folder asset proof is invalid")?;
        let expected = match proof.layout_version {
            1 => { let mut stem = doc_relative.to_path_buf(); stem.set_extension(""); proof.workspace_root.join("media").join(stem).join(filename) },
            2 => proof.workspace_root.join(".media").join(doc_relative.parent().ok_or("managed folder asset proof is invalid")?)
                .join(doc_relative.file_stem().filter(|v| !v.is_empty()).ok_or("managed folder asset proof is invalid")?).join(filename),
            _ => return Err("managed folder asset proof is invalid".into()),
        };
        if expected != proof.destination || !expected.starts_with(root)
            || protected(expected.strip_prefix(root).map_err(|_| "managed folder asset proof is invalid")?) {
            return Err("managed folder asset proof is invalid".into());
        }
        for checked in [&proof.document, &proof.path] {
            let relative = checked.strip_prefix(root).map_err(|_| "managed folder asset proof is invalid")?;
            if protected(relative) { return Err("managed folder asset proof is invalid".into()); }
        }
        let _document = read_folder_proof_file(root, proof.document.strip_prefix(root).map_err(|_| "managed folder asset proof is invalid")?)?;
        let bytes = read_folder_proof_file(root, proof.path.strip_prefix(root).map_err(|_| "managed folder asset proof is invalid")?)?;
        if format!("sha256:{:x}:{}", Sha256::digest(&bytes), bytes.len()) != proof.fingerprint { return Err("managed folder asset changed".into()); }
        expected.strip_prefix(root).map_err(|_| "managed folder asset proof is invalid")?.to_path_buf()
        }
    } else {
        let suffix = source_path.strip_prefix(source_folder).map_err(|_| "managed folder source is outside its subtree")?;
        if suffix.as_os_str().is_empty() { destination_folder.to_path_buf() } else { destination_folder.join(suffix) }
    };
    let target_text = target.to_str().ok_or("managed folder destination is invalid")?.replace('\\', "/");
    if !ResourceRegistry::validate_relative_path(&target_text) { return Err("managed folder destination is invalid".into()); }
    resolve_registered_path(actor_id, source, host_root, authorized_roots)?;
    // Validate every existing source component without following a symlink.
    let mut current = std::path::PathBuf::from(&canonical_root);
    for component in source_path.components() {
        let Component::Normal(name) = component else { return Err("managed folder source is invalid".into()); };
        current.push(name);
        let metadata = std::fs::symlink_metadata(&current).map_err(|_| "managed folder source is unavailable")?;
        if metadata.file_type().is_symlink() || !(metadata.is_file() || metadata.is_dir()) { return Err("managed folder source is unsupported".into()); }
    }
    if !std::fs::symlink_metadata(Path::new(&canonical_root).join(source_folder)).map_err(|_| "managed folder source is unavailable")?.is_dir() {
        return Err("managed folder source is not a directory".into());
    }
    let mut parent = std::path::PathBuf::from(&dest_root);
    for component in destination_folder.parent().ok_or("managed folder destination is invalid")?.components() {
        let Component::Normal(name) = component else { return Err("managed folder destination is invalid".into()); };
        parent.push(name);
        let metadata = std::fs::symlink_metadata(&parent).map_err(|_| "managed folder destination parent is unavailable")?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() { return Err("managed folder destination parent is unsupported".into()); }
    }
    match std::fs::symlink_metadata(Path::new(&dest_root).join(destination_folder)) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {},
        _ => return Err("managed folder destination is unavailable".into()),
    }
    let key = format!("{}\0{}\0{}", account_id, actor_id, operation_id);
    let request = if dest_id == root_id && dest_root == canonical_root { format!("{}\0{}\0{}\0{}", root_id, canonical_root, source_folder.display(), destination_folder.display()) } else { format!("{}\0{}\0{}\0{}\0{}\0{}", root_id, canonical_root, source_folder.display(), dest_id, dest_root, destination_folder.display()) };
    let operation_prefix = format!("file:folder:{:x}:", Sha256::digest(key.as_bytes()));
    let mapping_prefix = format!("{}{:x}:", operation_prefix, Sha256::digest(request.as_bytes()));
    let id = format!("{}{:x}", mapping_prefix, Sha256::digest(source_resource_id.as_bytes()));
    if registry.entries.keys().any(|existing| existing.starts_with(&operation_prefix) && !existing.starts_with(&mapping_prefix)) {
        return Err("managed folder registration changed; review required".into());
    }
    if let Some(existing) = registry.active_for_path(account_id, workspace_id, &dest_root, &target_text) {
        return if existing.resource_id == id { Ok((existing.clone(), false)) } else { Err("managed folder destination is already registered".into()) };
    }
    if registry.entries.contains_key(&id) { return Err("managed folder registration changed; review required".into()); }
    Ok((ResourceRegistration { resource_id:id, actor_ids:authorized_roots.iter().find(|root| root.root_id == dest_id)
        .map(|root| root.actor_ids.clone()).unwrap_or_default(), account_id:account_id.to_owned(), workspace_id:workspace_id.to_owned(),
        root_id:dest_id, root_path:dest_root, relative_path:target_text, active:true, generation:1, updated_millis:registry_timestamp() }, true))
}

/// The caller sees only its own exact subtree; incompatible aliases produce
/// one generic conflict without exposing the other registration.
fn registry_subtree_snapshot(
    registry: &ResourceRegistry, actor_id: &str, account_id: &str, workspace_id: &str,
    root_id: &str, root_path: &str, source_relative: &str, destination_relative: &str,
    destination_root_id: Option<&str>, destination_root_path: Option<&str>,
    host_root: &std::path::Path, authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<Vec<RegistrySubtreeEntry>, String> {
    use std::path::Path;
    if source_relative.contains('\\') || destination_relative.contains('\\')
        || !ResourceRegistry::validate_relative_path(source_relative) || !ResourceRegistry::validate_relative_path(destination_relative) {
        return Err("managed subtree is unavailable".into());
    }
    let source = Path::new(source_relative); let destination = Path::new(destination_relative);
    if source == Path::new(".") || destination == Path::new(".") || destination_root_id.is_some() != destination_root_path.is_some() {
        return Err("managed subtree is unavailable".into());
    }
    let (root_id, canonical_root) = registry_destination(actor_id, account_id, workspace_id,
        Some(root_id), root_path, ".", host_root, authorized_roots)?;
    let (dest_id, dest_root) = match (destination_root_id,destination_root_path) {
        (Some(id),Some(path)) => registry_destination(actor_id,account_id,workspace_id,Some(id),path,".",host_root,authorized_roots)?,
        (None,None) => (root_id.clone(),canonical_root.clone()),
        _ => return Err("managed subtree is unavailable".into()),
    };
    compatible_folder_roots(&canonical_root,&dest_root)?;
    let source_absolute = Path::new(&canonical_root).join(source);
    let destination_absolute = Path::new(&dest_root).join(destination);
    if source_absolute.starts_with(&destination_absolute) || destination_absolute.starts_with(&source_absolute) {return Err("managed subtree is unavailable".into());}
    let mut items = Vec::new();
    for entry in registry.entries.values().filter(|entry| entry.active) {
        if !ResourceRegistry::validate_relative_path(&entry.relative_path) { continue; }
        let absolute = Path::new(&entry.root_path).join(&entry.relative_path);
        if !absolute.starts_with(&source_absolute) && !absolute.starts_with(&destination_absolute) { continue; }
        let expected_root = if absolute.starts_with(&source_absolute) {(&root_id,&canonical_root)} else {(&dest_id,&dest_root)};
        if entry.account_id != account_id || entry.workspace_id != workspace_id
            || &entry.root_id != expected_root.0 || &entry.root_path != expected_root.1 {
            return Err("managed subtree is unavailable".into());
        }
        // This snapshot runs only for a new journal, before any destination
        // placeholder is registered. An existing target is a preflight conflict.
        if absolute.starts_with(&destination_absolute) {return Err("managed subtree destination is registered".into());}
        if absolute.starts_with(&source_absolute) {
            if entry.relative_path.contains('\\') || entry.generation == 0
                || resolve_registered_path(actor_id, entry, host_root, authorized_roots).is_err() {
                return Err("managed subtree is unavailable".into());
            }
            items.push(RegistrySubtreeEntry { resource_id:entry.resource_id.clone(), generation:entry.generation,
                relative_path:entry.relative_path.clone() });
            if items.len() > MAX_BATCH_RESOURCES { return Err("managed subtree exceeds its supported bound".into()); }
        }
    }
    Ok(items)
}

#[cfg(target_os = "linux")]
fn registry_managed_media_destination(
    actor_id: &str, account_id: &str, workspace_id: &str, requested_root_id: Option<&str>,
    root_path: &str, relative_path: &str, host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<(String, String), String> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::OpenOptionsExt;
    use std::path::Component;
    if !ResourceRegistry::validate_relative_path(relative_path) {
        return Err("managed media path is invalid".into());
    }
    let components: Vec<_> = std::path::Path::new(relative_path).components().collect();
    let v2 = components.iter().any(|part| matches!(part, Component::Normal(name) if *name == ".media"));
    let v1 = components.len() >= 3 && matches!(components[components.len() - 3], Component::Normal(name) if name == "media");
    if components.len() < 3 || !(v1 || v2) {
        return Err("managed pending destination is not workspace media".into());
    }
    if components.iter().any(|part| match part {
        Component::Normal(name) => matches!(name.to_string_lossy().to_ascii_lowercase().as_str(),
            ".env" | ".secrets" | "auth" | "credentials" | "tokens" | "settings"),
        _ => true,
    }) { return Err("managed media path is unavailable".into()); }
    let (root_id, canonical_root) = registry_destination(actor_id, account_id, workspace_id,
        requested_root_id, root_path, ".", host_root, authorized_roots)?;
    let mut options = std::fs::OpenOptions::new();
    options.read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC);
    let mut pinned = options.open(&canonical_root).map_err(|error| error.to_string())?;
    for part in &components[..components.len() - 1] {
        let Component::Normal(name) = part else { return Err("managed media path is invalid".into()); };
        let name = CString::new(name.as_bytes()).map_err(|_| "managed media path is invalid")?;
        // SAFETY: the parent descriptor and NUL-terminated component remain live.
        let fd = unsafe { libc::openat(pinned.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::NotFound { return Ok((root_id, canonical_root)); }
            return Err(format!("managed media ancestor is unavailable: {error}"));
        }
        // SAFETY: openat returned a fresh owned descriptor.
        pinned = unsafe { std::fs::File::from_raw_fd(fd) };
    }
    let Component::Normal(name) = components.last().ok_or("managed media path is invalid")? else {
        return Err("managed media path is invalid".into());
    };
    let name = CString::new(name.as_bytes()).map_err(|_| "managed media path is invalid")?;
    // SAFETY: the descriptor, name, and output buffer are valid for fstatat.
    let mut metadata: libc::stat = unsafe { std::mem::zeroed() };
    let result = unsafe { libc::fstatat(pinned.as_raw_fd(), name.as_ptr(), &mut metadata, libc::AT_SYMLINK_NOFOLLOW) };
    if result == 0 && (metadata.st_mode & libc::S_IFMT) == libc::S_IFLNK {
        return Err("managed media destination is a symlink".into());
    }
    if result != 0 && std::io::Error::last_os_error().kind() != std::io::ErrorKind::NotFound {
        return Err("managed media destination is unavailable".into());
    }
    Ok((root_id, canonical_root))
}

#[cfg(not(target_os = "linux"))]
fn registry_managed_media_destination(
    _actor_id: &str, _account_id: &str, _workspace_id: &str, _requested_root_id: Option<&str>,
    _root_path: &str, _relative_path: &str, _host_root: &std::path::Path,
    _authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<(String, String), String> {
    Err("managed pending media is unavailable on this platform".into())
}

fn registry_destination(
    actor_id: &str,
    account_id: &str,
    workspace_id: &str,
    requested_root_id: Option<&str>,
    root_path: &str,
    relative_path: &str,
    host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<(String, String), String> {
    if account_id.is_empty() || workspace_id.is_empty() {
        return Err("resource registration identity is incomplete".into());
    }
    if !ResourceRegistry::validate_relative_path(relative_path) {
        return Err("resource registration path is invalid".into());
    }
    let (root_id, root) = if authorized_roots.is_empty() {
        let root = ResourceRegistry::canonical_root(root_path, host_root)
            .map_err(|error| format!("resource registration root is unauthorized: {error}"))?;
        (requested_root_id.unwrap_or("host-root").to_owned(), root)
    } else {
        let requested_root_id = requested_root_id
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| "provider root identity is required".to_owned())?;
        let binding = authorized_roots
            .iter()
            .find(|binding| binding.root_id == requested_root_id)
            .ok_or_else(|| "provider root identity is unauthorized".to_owned())?;
        if (!binding.actor_ids.is_empty() && !binding.actor_ids.contains(actor_id))
            || (!binding.account_ids.is_empty() && !binding.account_ids.contains(account_id))
            || (!binding.workspace_ids.is_empty() && !binding.workspace_ids.contains(workspace_id))
        {
            return Err("provider root is unauthorized for this account/workspace".into());
        }
        restricted_child(binding, relative_path)?;
        let configured = std::fs::canonicalize(&binding.canonical_path)
            .map_err(|error| format!("provider root is unavailable: {error}"))?;
        if !matches!(binding.kind.as_str(), "exact_file" | "recursive_directory") {
            return Err("provider root kind is invalid".into());
        }
        if root_is_exact(binding) && relative_path != "." {
            return Err("exact-file provider roots require the empty relative path".into());
        }
        if root_is_exact(binding) && !configured.is_file() {
            return Err("exact-file provider root is not a file".into());
        }
        if !root_is_exact(binding) && !configured.is_dir() {
            return Err("recursive provider root is not a directory".into());
        }
        let requested = std::fs::canonicalize(root_path)
            .map_err(|error| format!("provider root is unavailable: {error}"))?;
        if configured != requested {
            return Err("provider root path does not match its trusted identity".into());
        }
        (binding.root_id.clone(), configured)
    };
    let candidate_path = if authorized_roots.is_empty() {
        root.join(relative_path)
    } else {
        let binding = authorized_roots
            .iter()
            .find(|binding| binding.root_id == root_id)
            .ok_or_else(|| "provider root identity is unauthorized".to_owned())?;
        if root_is_exact(binding) {
            root.clone()
        } else {
            root.join(relative_path)
        }
    };
    if let Ok(existing) = candidate_path.canonicalize() {
        if !existing.starts_with(&root) {
            return Err("resource registration resolves outside its root".into());
        }
    } else if let Some(parent) = candidate_path.parent() {
        let parent = parent
            .canonicalize()
            .map_err(|error| format!("resource registration parent is unavailable: {error}"))?;
        if !parent.starts_with(&root) {
            return Err("resource registration parent resolves outside its root".into());
        }
    }
    if let Some(binding) = authorized_roots.iter().find(|binding| binding.root_id == root_id) {
        if let Some(child) = &binding.selected_child {
            let selected = root.join(child);
            if std::fs::symlink_metadata(&selected).is_ok() {
                let selected_actual = selected.canonicalize().map_err(|_| "selected child unavailable")?;
                if selected_actual != selected { return Err("selected child alias unavailable".into()); }
                if let Ok(actual) = candidate_path.canonicalize() {
                    if !actual.starts_with(&selected_actual) { return Err("selected child alias unavailable".into()); }
                }
            } else if candidate_path != selected { return Err("selected child unavailable".into()); }
        }
    }
    Ok((root_id, root.to_string_lossy().into_owned()))
}

fn resolve_registered_path(
    actor_id: &str,
    entry: &ResourceRegistration,
    host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<std::path::PathBuf, String> {
    if !entry.actor_ids.is_empty() && !entry.actor_ids.contains(actor_id) { return Err("registered provider resource is unauthorized for this actor".into()); }
    let root = if authorized_roots.is_empty() {
        ResourceRegistry::canonical_root(&entry.root_path, host_root)
            .map_err(|error| format!("registered root is unauthorized: {error}"))?
    } else {
        let binding = authorized_roots
            .iter()
            .find(|binding| binding.root_id == entry.root_id)
            .ok_or_else(|| "registered provider root is no longer authorized".to_owned())?;
        if (!binding.actor_ids.is_empty() && !binding.actor_ids.contains(actor_id))
            || (!binding.account_ids.is_empty() && !binding.account_ids.contains(&entry.account_id))
            || (!binding.workspace_ids.is_empty()
                && !binding.workspace_ids.contains(&entry.workspace_id))
        {
            return Err(
                "registered provider root is no longer authorized for this account/workspace"
                    .into(),
            );
        }
        restricted_child(binding, &entry.relative_path)?;
        let configured = std::fs::canonicalize(&binding.canonical_path)
            .map_err(|error| format!("registered provider root is unavailable: {error}"))?;
        let recorded = std::fs::canonicalize(&entry.root_path)
            .map_err(|error| format!("registered provider root is unavailable: {error}"))?;
        if configured != recorded {
            return Err("registered provider root changed identity".into());
        }
        if !matches!(binding.kind.as_str(), "exact_file" | "recursive_directory") {
            return Err("registered provider root kind is invalid".into());
        }
        if root_is_exact(binding) && entry.relative_path != "." {
            return Err("registered exact-file resource has a non-empty relative path".into());
        }
        if root_is_exact(binding) && !configured.is_file() {
            return Err("registered exact-file provider root is not a file".into());
        }
        if !root_is_exact(binding) && !configured.is_dir() {
            return Err("registered recursive provider root is not a directory".into());
        }
        configured
    };
    if !ResourceRegistry::validate_relative_path(&entry.relative_path) {
        return Err("registered resource path is invalid".into());
    }
    let candidate = if authorized_roots
        .iter()
        .find(|binding| binding.root_id == entry.root_id)
        .is_some_and(root_is_exact)
    {
        root.clone()
    } else {
        root.join(&entry.relative_path)
    };
    let resolved = match std::fs::canonicalize(&candidate) {
        Ok(path) => path,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let parent = candidate
                .parent()
                .ok_or_else(|| "registered resource has no parent".to_owned())?;
            std::fs::canonicalize(parent)
                .map_err(|error| format!("registered resource parent is unavailable: {error}"))?
                .join(
                    candidate
                        .file_name()
                        .ok_or_else(|| "registered resource has no filename".to_owned())?,
                )
        }
        Err(error) => return Err(error.to_string()),
    };
    if !resolved.starts_with(&root) {
        return Err("registered resource resolves outside its root".into());
    }
    if let Some(binding) = authorized_roots.iter().find(|binding| binding.root_id == entry.root_id) {
        if let Some(child) = &binding.selected_child {
            let selected = root.join(child);
            if selected.exists() {
                let actual = selected.canonicalize().map_err(|_| "selected child unavailable")?;
                if actual != selected || !resolved.starts_with(&actual) { return Err("selected child alias unavailable".into()); }
            } else if resolved != selected { return Err("selected child unavailable".into()); }
        }
    }
    Ok(resolved)
}

#[cfg(unix)]
async fn regular_file_source_v1(coordinator:&HistoryCoordinator,key:&openclank_history::catalog::ResourceKey,source:&openclank_history::protocol::RegularFileSourceV1,path:&std::path::Path,receipt_root:&std::path::Path) -> Result<(String,openclank_history::catalog::VersionReceipt,openclank_history::restore::RegularFileStateV1,Option<String>),String> {
    use openclank_history::catalog::VersionContent;
    use openclank_history::protocol::RegularFileSourceV1;
    use openclank_history::restore::{RegularFileJournalV1,RegularFileJournalStateV1,RegularFileStateV1,regular_file_effect_digest_v1};
    match source {
        RegularFileSourceV1::Selected{version_ref}=>{
            let (descriptor,receipt,mode)=coordinator.resolve_regular_state_version(key,version_ref).map_err(|_|"source unavailable")?;
            let bytes=coordinator.read_version(&descriptor.action_id,&receipt).await.map_err(|_|"source unavailable")?;
            let state=match (&receipt.content,mode,bytes) {
                (VersionContent::Tombstone,None,None)=>RegularFileStateV1::Absent,
                (content,Some(mode),Some(bytes)) if !matches!(content,VersionContent::Tombstone) && bytes.len()<=16*1024*1024=>RegularFileStateV1::Present{sha256:format!("{:x}",Sha256::digest(&bytes)),size:bytes.len() as u64,mode},
                _=>return Err("source unavailable".into()),
            };
            Ok((descriptor.action_id,receipt,state,None))
        },
        RegularFileSourceV1::Inverse{restore_id}=>{
            if restore_id.is_empty() || restore_id.len()>256{return Err("source unavailable".into())}
            let original=coordinator.catalog().get_restore::<RegularFileJournalV1>(restore_id).map_err(|_|"source unavailable")?.ok_or("source unavailable")?;
            let inverse=original.inverse.as_ref().ok_or("source unavailable")?;
            let proven=original.state==RegularFileJournalStateV1::RegularFileV1Complete || (original.state==RegularFileJournalStateV1::RegularFileV1Applying && FilesystemRestoreProvider::new_with_receipt_root(path,receipt_root).regular_file_receipt_verified_v1(&original).unwrap_or(false));
            if original.restore_id!=*restore_id || original.version!=1 || original.consistency!="optimistic_path_guard" || original.destination!=*key || !proven || original.effect_digest!=regular_file_effect_digest_v1(&original).map_err(|_|"source unavailable")? || inverse.action_id!=format!("{restore_id}:pre-restore") || inverse.state!=original.expected.state {return Err("source unavailable".into())}
            if coordinator.catalog().expiry(&inverse.action_id).map_err(|_|"source unavailable")?.is_some_and(|e|matches!(e.state,openclank_history::retention::ExpiryState::Expiring|openclank_history::retention::ExpiryState::Expired)) {return Err("source unavailable".into())}
            let bytes=coordinator.read_version(&inverse.action_id,&inverse.receipt).await.map_err(|_|"source unavailable")?;
            match (&inverse.state,&inverse.receipt.content,bytes.as_deref()) {
                (RegularFileStateV1::Absent,VersionContent::Tombstone,None)=>{},
                (RegularFileStateV1::Present{sha256,size,mode},content,Some(bytes)) if !matches!(content,VersionContent::Tombstone) && *mode<=0o777 && *size==bytes.len() as u64 && *sha256==format!("{:x}",Sha256::digest(bytes))=>{},
                _=>return Err("source unavailable".into()),
            }
            Ok((inverse.action_id.clone(),inverse.receipt.clone(),inverse.state.clone(),Some(restore_id.clone())))
        }
    }
}

async fn restore_registered_resource_version(
    coordinator: &HistoryCoordinator,
    envelope: ControlEnvelope,
    restore_id: String,
    resource_id: String,
    version_ref: String,
    expected_destination_fingerprint: String,
    auth_bindings: &AuthBindings,
    grants: &[(String, String)],
    resource_map: &std::path::Path,
    host_root: &std::path::Path,
    receipt_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
) -> ServiceResponse {
    if let Err(error) = auth_bindings.validate_control_cap(&envelope, "restore") {
        return ServiceResponse::Error {
            code: format!("{error:?}"),
        };
    }
    if restore_id.trim().is_empty()
        || restore_id.len() > 256
        || envelope.action_id != restore_id
        || expected_destination_fingerprint.trim().is_empty()
        || expected_destination_fingerprint.len() > 256
    {
        return ServiceResponse::Error {
            code: "selected restore requires a matching restore id and reviewed destination fingerprint".into(),
        };
    }
    let (destination_key, _handle, registration) = match registered_files_resource_key(
        &resource_id,
        &envelope.auth,
        grants,
        resource_map,
        host_root,
        authorized_roots,
        true,
    ) {
        Ok(value) => value,
        Err(code) => {
            return ServiceResponse::Unavailable {
                code: code.into(),
                message: "registered Files resource is missing or unavailable".into(),
            }
        }
    };
    if destination_key.account_id != envelope.auth.account_id {
        return ServiceResponse::Unavailable {
            code: "resource_unavailable".into(),
            message: "restore requires an account-owned Files resource".into(),
        };
    }
    let (source_version, source_receipt) = match coordinator
        .resolve_resource_version(&destination_key, &version_ref)
    {
        Ok(selection) => selection,
        Err(error) => {
            return ServiceResponse::Unavailable {
                code: error.code().into(),
                message: error.message(),
            }
        }
    };
    let destination_path = match resolve_registered_path(
        &envelope.auth.actor_id,
        &registration,
        host_root,
        authorized_roots,
    ) {
        Ok(path) => path,
        Err(_) => {
            return ServiceResponse::Unavailable {
                code: "resource_unavailable".into(),
                message: "registered Files destination is unavailable".into(),
            }
        }
    };
    let mut provider = FilesystemRestoreProvider::new_with_receipt_root(
        &destination_path,
        receipt_root,
    );
    let observed_destination_fingerprint = match provider.current_fingerprint() {
        Ok(fingerprint) => fingerprint,
        Err(_) => {
            return ServiceResponse::Unavailable {
                code: "resource_unavailable".into(),
                message: "registered Files destination cannot be read".into(),
            }
        }
    };
    let observed_for_review = observed_destination_fingerprint
        .as_deref()
        .unwrap_or("missing");
    let prior_restore = coordinator.catalog().get_restore::<openclank_history::restore::RestoreJournal>(&restore_id);
    if prior_restore.is_err() { return ServiceResponse::Error { code: "restore journal unavailable".into() }; }
    if prior_restore.ok().flatten().is_none() && expected_destination_fingerprint != observed_for_review {
        return ServiceResponse::Error {
            code: "restore outcome: Conflict".into(),
        };
    }
    let request = RestoreRequest {
        restore_id,
        account_id: destination_key.account_id.clone(),
        source_action_id: source_version.action_id,
        source_version_id: source_version.version_id,
        destination: destination_key,
        expected_destination_fingerprint: if expected_destination_fingerprint == "missing" {
            None
        } else {
            Some(expected_destination_fingerprint)
        },
        require_current_capture: true,
    };
    let plan = match prepare_restore_authorized(
        coordinator,
        &request,
        &source_receipt,
        observed_destination_fingerprint.as_deref(),
    )
    .await
    {
        Ok(plan) => plan,
        Err(outcome) => {
            return ServiceResponse::Error {
                code: format!("restore outcome: {outcome:?}"),
            }
        }
    };
    match apply_restore_authorized_durable(coordinator, &request, &plan, &mut provider).await {
        Ok(receipt) => ServiceResponse::Restore(receipt),
        Err(outcome) => ServiceResponse::Error {
            code: format!("restore outcome: {outcome:?}"),
        },
    }
}

fn resource_handle(registration: &ResourceRegistration) -> ResourceHandle {
    ResourceHandle {
        resource_id: registration.resource_id.clone(),
        account_id: registration.account_id.clone(),
        workspace_id: registration.workspace_id.clone(),
        generation: registration.generation,
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    use bytes::Bytes;
    use std::env;
    use std::io::Write;
    use std::sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc,
    };
    use tokio::io::{AsyncWriteExt, BufReader};


    let args: Vec<String> = env::args().collect();
    let socket = args
        .get(1)
        .ok_or("usage: service <socket> <catalog> <lore> <account>")?;
    let catalog = args.get(2).ok_or("missing catalog")?;
    let lore = args.get(3).ok_or("missing lore")?;
    let legacy_account = args
        .get(4)
        .map(String::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(ToOwned::to_owned);
    let host_root = args
        .get(5)
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    let receipt_root = args
        .get(6)
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| host_root.join(".openclank-history-receipts"));
    // Host resources are registered by the service owner.  Restore requests
    // carry only the opaque ResourceKey id; the path is checked against this
    // map before a provider is constructed.  Keeping the map beside the
    // service-owned receipt root prevents callers from choosing an arbitrary
    // file underneath the broad host root.
    let resource_map = args
        .get(7)
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| receipt_root.join("host-resources.json"));
    // Validate/initialize Lore before auxiliary service paths (such as
    // staging beside a resource map under lore_root) can make a fresh root
    // appear to contain unknown preexisting data. The same guarded check runs
    // again when HistoryCoordinator opens the store below.
    openclank_history::HistoryStore::initialize_root(lore)?;
    let staging_root = resource_map
        .parent()
        .unwrap_or_else(|| std::path::Path::new("."))
        .join(".openclank-history-staging");
    std::fs::create_dir_all(&staging_root)?;
    openclank_history::platform::protect_private_directory(&staging_root)?;
    if let Ok(entries) = std::fs::read_dir(&staging_root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| {
                    name.starts_with(".openclank-stage-") && name.ends_with(".part")
                        && !name.starts_with(".openclank-stage-odysseus-checkpoint-")
                })
            {
                let _ = std::fs::remove_file(path);
            }
        }
    }
    let mut authorized_roots = env::var("OPENCLANK_HISTORY_AUTHORIZED_ROOTS")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .map(|raw| {
            serde_json::from_str::<Vec<AuthorizedHistoryRoot>>(&raw)
                .map_err(|error| format!("invalid OPENCLANK_HISTORY_AUTHORIZED_ROOTS: {error}"))
        })
        .transpose()?
        .unwrap_or_default();
    let authorized_roots_file =
        env::var_os("OPENCLANK_HISTORY_AUTHORIZED_ROOTS_FILE").map(std::path::PathBuf::from);
    let authority_file =
        env::var_os("OPENCLANK_HISTORY_AUTHORITY_FILE").map(std::path::PathBuf::from);
    let legacy_token = env::var("OPENCLANK_HISTORY_TOKEN").ok();
    let credentials_file =
        env::var_os("OPENCLANK_HISTORY_CREDENTIALS_FILE").map(std::path::PathBuf::from);
    let credentials_json = credentials_file
        .as_ref()
        .map(std::fs::read_to_string)
        .transpose()
        .map_err(|error| format!("cannot read server-issued history credentials: {error}"))?
        .or_else(|| env::var("OPENCLANK_HISTORY_CREDENTIALS").ok());
    let mut credentials = credentials_json
        .and_then(|raw| Some(raw))
        .map(|raw| serde_json::from_str::<Vec<ServerCredential>>(&raw))
        .transpose()
        .map_err(|error| format!("invalid OPENCLANK_HISTORY_CREDENTIALS: {error}"))?
        .unwrap_or_default();
    if let Some(path) = authority_file.as_deref() {
        if let Ok(raw) = std::fs::read_to_string(path) {
            if let Ok(manifest) = serde_json::from_str::<AuthorityManifest>(&raw) {
                credentials = manifest.credentials;
                authorized_roots = manifest.authorized_roots;
            }
        }
    }
    credentials.retain(|binding| {
        !binding.actor_id.trim().is_empty()
            && !binding.account_id.trim().is_empty()
            && !binding.token.trim().is_empty()
    });
    if credentials.is_empty() && (legacy_account.is_none() || legacy_token.is_none()) {
        return Err("server-issued history credentials are required".into());
    }
    let auth_bindings = AuthBindings {
        credentials: credentials.clone(),
        legacy_account: legacy_account.clone(),
        legacy_token: legacy_token.clone(),
        authority_generation: None,
    };
    let legacy_grants = if credentials.is_empty() {
        env::var("OPENCLANK_HISTORY_OWNER_GRANTS")
            .unwrap_or_default()
            .split(',')
            .filter_map(|entry| {
                let (actor, owner) = entry.split_once(':')?;
                Some((actor.to_owned(), owner.to_owned()))
            })
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let account = legacy_account
        .clone()
        .or_else(|| {
            credentials
                .first()
                .map(|binding| binding.account_id.clone())
        })
        .unwrap_or_else(|| "bootstrap".into());
    let mut listener = openclank_history::platform::Listener::bind(socket)?;
    let coordinator = Arc::new(tokio::sync::Mutex::new(
        HistoryCoordinator::open(catalog, lore, &account).await?,
    ));
    let staged_uploads = Arc::new(tokio::sync::Mutex::new(
        BTreeMap::<String, StagedUpload>::new(),
    ));
    let staging_quota = env::var("OPENCLANK_HISTORY_STAGING_QUOTA_BYTES")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_STAGING_QUOTA_BYTES);
    let staging_ttl_millis = env::var("OPENCLANK_HISTORY_STAGING_TTL_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_STAGING_TTL_MILLIS);
    let max_active_stages = env::var("OPENCLANK_HISTORY_MAX_ACTIVE_STAGES")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_MAX_ACTIVE_STAGES);
    let staged_bytes = Arc::new(AtomicU64::new(0));
    let staging_admission = Arc::new(tokio::sync::Mutex::new(()));
    let stopping = Arc::new(AtomicBool::new(false));
    let resource_digest_slots = Arc::new(tokio::sync::Semaphore::new(2));
    // Every admitted frame retains a read lease through its effects. A barrier
    // takes the write lease before reloading authority and acknowledging it.
    let authority_gate = Arc::new(tokio::sync::RwLock::new(()));
    let maintenance = coordinator.clone();
    let maintenance_stop = stopping.clone();
    tokio::spawn(async move {
        while !maintenance_stop.load(Ordering::Acquire) {
            if let Err(error) = maintenance.lock().await.maintain_capture_retention().await {
                eprintln!("History retention deferred: {error}");
            }
            tokio::time::sleep(std::time::Duration::from_secs(60)).await;
        }
    });
    while !stopping.load(Ordering::Acquire) {
        let stream =
            match tokio::time::timeout(std::time::Duration::from_millis(100), listener.accept())
                .await
            {
                Ok(result) => result?,
                Err(_) => continue,
            };
        let coordinator = coordinator.clone();
        let stopping = stopping.clone();
        let authority_gate = authority_gate.clone();
        let mut auth_bindings = auth_bindings.clone();
        let legacy_grants = legacy_grants.clone();
        let credentials_file = credentials_file.clone();
        let authorized_roots_file = authorized_roots_file.clone();
        let authority_file = authority_file.clone();
        let host_root = host_root.clone();
        let receipt_root = receipt_root.clone();
        let resource_map = resource_map.clone();
        let mut authorized_roots = authorized_roots.clone();
        let staging_root = staging_root.clone();
        let staged_uploads = staged_uploads.clone();
        let staged_bytes = staged_bytes.clone();
        let staging_admission = staging_admission.clone();
        let resource_digest_slots = resource_digest_slots.clone();
        tokio::spawn(async move {
            let (read, mut write) = tokio::io::split(stream);
            let mut reader = BufReader::new(read);
            loop {
                let frame = match read_frame(&mut reader).await {
                    Ok(Some(frame)) => frame,
                    _ => break,
                };
                if let Ok(ServiceRequest::AgentAuthorityBarrier { envelope, root_id, phase, drain_session_id, expected_generation, expected_process_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let _exclusive = authority_gate.write().await;
                    let mut current = auth_bindings.clone();
                    let response = if authority_file.is_none() {
                        ServiceResponse::Error { code: "agent authority barrier unavailable".into() }
                    } else {
                        let (_, roots) = current.reload_authority_file(authority_file.as_deref());
                        let matching: Vec<_> = roots.iter().filter(|root| root.root_id == root_id).collect();
                        let binding = current.authenticate(&envelope.auth).and_then(|binding| {
                            protocol_validate_control(&envelope, &binding.account_id, &binding.token)?;
                            Ok(binding)
                        });
                        match binding {
                            Ok(binding) if binding.actor_id == envelope.auth.actor_id
                                && binding.actor_id.starts_with("agent:") && binding.actor_id.len() > "agent:".len()
                                && root_id.starts_with("skill-history-")
                                && current.authority_generation == Some(expected_generation)
                                && std::process::id() == expected_process_id
                                && ((phase == "present" && drain_session_id.is_none()
                                    && binding.checkpoint_root_id.is_none() && binding.drain_session_id.is_none()
                                    && (binding.capabilities == BTreeSet::from(["read".to_owned()])
                                        || binding.capabilities == BTreeSet::from(["capture".to_owned(), "read".to_owned()]))
                                    && binding.owner_accounts.is_empty() && matching.len() == 1
                                    && matching[0].account_ids == BTreeSet::from([binding.account_id.clone()])
                                    && matching[0].actor_ids == BTreeSet::from([binding.actor_id.clone()])
                                    && matching[0].workspace_ids.len() == 1
                                    && matching[0].selected_child.as_deref().is_some_and(|child| restricted_child(matching[0], child).is_ok()))
                                  || (phase == "absent" && matching.is_empty()
                                    && binding.checkpoint_root_id.as_deref() == Some(root_id.as_str())
                                    && binding.capabilities == BTreeSet::from(["skill-draft-drain".to_owned()])
                                    && binding.owner_accounts.is_empty()
                                    && drain_session_id.as_deref().is_some_and(|id| id.len() == 64 && id.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)))
                                    && binding.drain_session_id == drain_session_id)) => ServiceResponse::AgentAuthorityBarrier {
                                        authority_generation: expected_generation, process_id: expected_process_id,
                                    },
                            _ => ServiceResponse::Error { code: "agent authority barrier unavailable".into() },
                        }
                    };
                    let encoded = match serde_json::to_string(&response) { Ok(value) => value, Err(_) => break };
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err() { break; }
                    continue;
                }
                if let Ok(ServiceRequest::AuthorityBarrier { envelope, expected_generation, expected_process_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let _exclusive = authority_gate.write().await;
                    let mut current = auth_bindings.clone();
                    let response = if authority_file.is_none() {
                        ServiceResponse::Error { code: "authority barrier unavailable".into() }
                    } else {
                        current.reload_authority_file(authority_file.as_deref());
                        match current.validate_control_cap(&envelope, "read") {
                            Ok(binding) if binding.actor_id == envelope.auth.actor_id
                                && binding.actor_id != "*" && !binding.actor_id.starts_with("agent:")
                                && current.authority_generation == Some(expected_generation)
                                && std::process::id() == expected_process_id => ServiceResponse::AuthorityBarrier {
                                    authority_generation: expected_generation, process_id: expected_process_id,
                                },
                            _ => ServiceResponse::Error { code: "authority barrier unavailable".into() },
                        }
                    };
                    let encoded = match serde_json::to_string(&response) { Ok(value) => value, Err(_) => break };
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err() { break; }
                    continue;
                }
                let _frame_lease = authority_gate.read().await;
                {
                    let mut uploads = staged_uploads.lock().await;
                    reap_staged_uploads(
                        &mut uploads,
                        registry_timestamp(),
                        staging_ttl_millis,
                        &staged_bytes,
                    );
                }
                // Credentials are published by the supervisor with an atomic replace. Reload
                // before every frame so account revoke/rename takes effect without restarting
                // the worker or dropping an in-flight connection.
                let (removed_accounts, authority_roots) = if authority_file.is_some() {
                    auth_bindings.reload_authority_file(authority_file.as_deref())
                } else {
                    (
                        auth_bindings.reload_credentials_file(credentials_file.as_deref()),
                        Vec::new(),
                    )
                };
                if authority_file.is_some() {
                    authorized_roots = authority_roots;
                }
                if !removed_accounts.is_empty() {
                    let coordinator_guard = coordinator.lock().await;
                    for account_id in removed_accounts {
                        let _ = coordinator_guard.release_account_leases(&account_id);
                    }
                }
                let grants = auth_bindings.grants(&legacy_grants);
                if authority_file.is_none() {
                    if let Some(path) = authorized_roots_file.as_deref() {
                        match std::fs::read_to_string(path).ok().and_then(|raw| {
                            serde_json::from_str::<Vec<AuthorizedHistoryRoot>>(&raw).ok()
                        }) {
                            Some(updated) => authorized_roots = updated,
                            None => authorized_roots.clear(),
                        }
                    }
                }
                // This read-only digest leaves the coordinator lock before Lore I/O.
                // The owned task retains the existing Catalog resource lease and
                // capacity permit until the internal stream has fully drained.
                if let Ok(ServiceRequest::DigestResourceVersion { envelope, resource_id, version_ref }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let deadline=tokio::time::Instant::now()+std::time::Duration::from_secs(5);
                    let result = async {
                        auth_bindings.validate_control_cap(&envelope,"read").map_err(|_| "digest_unavailable")?;
                        let (key,_,_) = registered_files_resource_key(&resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,true).map_err(|_| "digest_unavailable")?;
                        let permit=resource_digest_slots.clone().try_acquire_owned().map_err(|_| "digest_busy")?;
                        let guard=tokio::time::timeout_at(deadline,coordinator.lock()).await.map_err(|_| "digest_timeout")?;
                        let plan=guard.prepare_resource_digest(&key,&version_ref).map_err(|_| "digest_unavailable")?;
                        drop(guard);
                        if tokio::time::Instant::now()>=deadline { return Err("digest_timeout"); }
                        let mut task=tokio::spawn(async move {let _permit=permit; plan.digest().await});
                        let (version,sha256,size)=tokio::time::timeout_at(deadline,&mut task).await.map_err(|_| "digest_timeout")?
                            .map_err(|_| "digest_unavailable")?.map_err(|_| "digest_unavailable")?;
                        let (current,_,_)=registered_files_resource_key(&resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,true).map_err(|_| "digest_unavailable")?;
                        if current!=key {return Err("digest_unavailable");}
                        Ok(ServiceResponse::ResourceVersionDigest {version,sha256,size})
                    }.await;
                    let response=result.unwrap_or_else(|code|ServiceResponse::Unavailable{code:code.into(),message:"Recorded version digest unavailable".into()});
                    let encoded=match serde_json::to_string(&response){Ok(value)=>value,Err(_)=>break};
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}
                    continue;
                }
                // Typed regular-file requests are a distinct discriminator;
                // an older service cannot silently accept them as byte restore.
                let regular_request=match serde_json::from_slice::<ServiceRequest>(&frame) {
                    Ok(req @ ServiceRequest::PreviewRegularFileV1{..}) | Ok(req @ ServiceRequest::ExecuteRegularFileV1{..}) | Ok(req @ ServiceRequest::GetRegularFileStatusV1{..})=>Some(req),_=>None};
                if let Some(regular_request)=regular_request {
                    #[cfg(unix)]
                    let response={
                        use openclank_history::restore::{RegularFileJournalV1,RegularFileJournalStateV1,RegularFileRefusalV1,RegularFileRefusalStateV1,RegularFileObservationV1,regular_file_effect_digest_v1,observe_regular_file_v1,apply_regular_file_authorized_v1};
                        let coordinator=coordinator.lock().await;
                        let result=async {
                            let (envelope,resource_id,source,restore_id,expected,mode)=match regular_request {
                                ServiceRequest::PreviewRegularFileV1{envelope,resource_id,source}=>(envelope,resource_id,source,None,None,0u8),
                                ServiceRequest::ExecuteRegularFileV1{envelope,restore_id,resource_id,source,expected}=>(envelope,resource_id,source,Some(restore_id),Some(expected),1),
                                ServiceRequest::GetRegularFileStatusV1{envelope,restore_id,resource_id,source,expected}=>(envelope,resource_id,source,Some(restore_id),Some(expected),2),
                                _=>return Err("regular state unavailable".to_owned()),
                            };
                            auth_bindings.validate_control_cap(&envelope,if mode==2{"read"}else{"restore"}).map_err(|_|"regular state unavailable")?;
                            if restore_id.as_ref().is_some_and(|id|id.is_empty() || id.len()>256 || envelope.action_id!=*id) {return Err("regular state unavailable".into())}
                            let (key,_,entry)=registered_files_resource_key_in(&resource_id,&envelope.auth,&grants,&ResourceRegistry::load_bounded(&resource_map,4*1024*1024).map_err(|_|"regular state unavailable")?,&host_root,&authorized_roots,true).map_err(|_|"regular state unavailable")?;
                            if key.account_id!=envelope.auth.account_id {return Err("regular state unavailable".into())}
                            let path=resolve_registered_path(&envelope.auth.actor_id,&entry,&host_root,&authorized_roots).map_err(|_|"regular state unavailable")?;
                            let selector_hash=format!("{:x}",Sha256::digest(serde_json::to_vec(&source).map_err(|_|"regular state unavailable")?));
                            let read_status=|journal:&RegularFileJournalV1,expected:&RegularFileObservationV1,id:&str| -> Result<ServiceResponse,String> {
                                if journal.version!=1 || journal.consistency!="optimistic_path_guard" || journal.restore_id!=id || journal.destination!=key || journal.source_selector_hash!=selector_hash || journal.expected!=*expected || journal.effect_digest!=regular_file_effect_digest_v1(journal).map_err(|_|"regular state unavailable")? {return Err("regular state conflict".into())}
                                let (state,verified)=match &journal.state {
                                    RegularFileJournalStateV1::RegularFileV1Applying if FilesystemRestoreProvider::new_with_receipt_root(&path,&receipt_root).regular_file_receipt_verified_v1(journal).unwrap_or(false)=>("complete",true),
                                    RegularFileJournalStateV1::RegularFileV1Prepared|RegularFileJournalStateV1::RegularFileV1Applying=>("pending",false),
                                    RegularFileJournalStateV1::RegularFileV1Complete if journal.inverse.is_some()=>("complete",true),
                                    RegularFileJournalStateV1::RegularFileV1Conflict=>("conflict",false),_ =>("unknown",false),
                                };
                                let current_matches=observe_regular_file_v1(&path).ok().map(|(current,_)|current.state==journal.desired);
                                Ok(ServiceResponse::RegularFileStatusV1{state:state.into(),verified,current_matches,consistency:"optimistic_path_guard".into()})
                            };
                            let read_original=|value:serde_json::Value,expected:&RegularFileObservationV1,id:&str| -> Result<ServiceResponse,String> {
                                if let Ok(journal)=serde_json::from_value::<RegularFileJournalV1>(value.clone()) {return read_status(&journal,expected,id)}
                                let refusal:RegularFileRefusalV1=serde_json::from_value(value).map_err(|_|"regular state conflict")?;
                                if refusal.version!=1 || refusal.state!=RegularFileRefusalStateV1::RegularFileV1Refused || refusal.consistency!="optimistic_path_guard" || refusal.restore_id!=id || refusal.destination!=key || refusal.source_selector_hash!=selector_hash || refusal.expected!=*expected {return Err("regular state conflict".into())}
                                Ok(ServiceResponse::RegularFileStatusV1{state:"conflict".into(),verified:false,current_matches:None,consistency:"optimistic_path_guard".into()})
                            };
                            let refuse=|id:&str,expected:&RegularFileObservationV1| -> Result<ServiceResponse,String> {
                                let refusal=RegularFileRefusalV1{version:1,state:RegularFileRefusalStateV1::RegularFileV1Refused,consistency:"optimistic_path_guard".into(),restore_id:id.to_owned(),destination:key.clone(),source_selector_hash:selector_hash.clone(),expected:expected.clone()};
                                if coordinator.catalog().compare_restore_exact(id,None,&refusal).map_err(|_|"regular state unavailable")? {return Ok(ServiceResponse::RegularFileStatusV1{state:"conflict".into(),verified:false,current_matches:None,consistency:"optimistic_path_guard".into()})}
                                let original=coordinator.catalog().get_restore::<serde_json::Value>(id).map_err(|_|"regular state unavailable")?.ok_or("regular state unavailable")?;
                                read_original(original,expected,id)
                            };
                            if let Some(id)=restore_id.as_ref() {
                                match coordinator.catalog().get_restore::<serde_json::Value>(id) {
                                    Ok(Some(original))=>return read_original(original,expected.as_ref().ok_or("regular state unavailable")?,id),
                                    Err(_)=>return Err("regular state conflict".into()),
                                    Ok(None) if mode==2=>return Ok(ServiceResponse::RegularFileStatusV1{state:"unknown".into(),verified:false,current_matches:None,consistency:"optimistic_path_guard".into()}),
                                    Ok(None)=>{},
                                }
                            }
                            let (source_action_id,receipt,desired,inverse_of)=match regular_file_source_v1(&coordinator,&key,&source,&path,&receipt_root).await {
                                Ok(value)=>value,Err(_) if mode==1=>return refuse(restore_id.as_deref().ok_or("regular state unavailable")?,expected.as_ref().ok_or("regular state unavailable")?),Err(error)=>return Err(error),
                            };
                            let (observed,_)=match observe_regular_file_v1(&path) {Ok(value)=>value,Err(_) if mode==1=>return refuse(restore_id.as_deref().ok_or("regular state unavailable")?,expected.as_ref().ok_or("regular state unavailable")?),Err(_)=>return Err("regular state unavailable".into())};
                            // A removed (inactive) registration may be restored only while its leaf is absent: nothing present is ever replaced through it.
                            if !regular_restore_state_allowed(entry.active,&observed) {
                                if mode==1 {return refuse(restore_id.as_deref().ok_or("regular state unavailable")?,expected.as_ref().ok_or("regular state unavailable")?)}
                                if mode==0 {return Err("regular state unavailable".into())}
                            }
                            if mode==0 {return Ok(ServiceResponse::RegularFilePreviewV1(openclank_history::protocol::RegularFilePreviewV1{version:1,consistency:"optimistic_path_guard".into(),source_action_id,source_version_id:receipt.version_id,desired,expected:observed}))}
                            let id=restore_id.ok_or("regular state unavailable")?;let expected=expected.ok_or("regular state unavailable")?;
                            if expected!=observed {return refuse(&id,&expected)}
                            let mut journal=RegularFileJournalV1{version:1,consistency:"optimistic_path_guard".into(),restore_id:id.clone(),source_action_id:source_action_id.clone(),source_version_id:receipt.version_id.clone(),source_selector_hash:selector_hash.clone(),inverse_of:inverse_of.clone(),destination:key.clone(),expected,desired,inverse:None,effect_digest:String::new(),state:RegularFileJournalStateV1::RegularFileV1Prepared};
                            journal.effect_digest=regular_file_effect_digest_v1(&journal).map_err(|_|"regular state unavailable")?;
                            if !coordinator.catalog().compare_restore_exact(&id,None,&journal).map_err(|_|"regular state unavailable")? {
                                let original=coordinator.catalog().get_restore::<serde_json::Value>(&id).map_err(|_|"regular state unavailable")?.ok_or("regular state unavailable")?;
                                return read_original(original,&journal.expected,&id)
                            }
                            let request=RestoreRequest{restore_id:id.clone(),account_id:key.account_id.clone(),source_action_id,source_version_id:receipt.version_id.clone(),destination:key.clone(),expected_destination_fingerprint:None,require_current_capture:true};
                            let mut provider=FilesystemRestoreProvider::new_with_receipt_root(&path,&receipt_root);
                            let result=apply_regular_file_authorized_v1(&coordinator,&request,&receipt,&mut provider,inverse_of.as_deref()).await;
                            let mut final_journal=match result {Ok(journal)=>journal,Err(_)=>coordinator.catalog().get_restore::<RegularFileJournalV1>(&id).map_err(|_|"regular state unavailable")?.ok_or("regular state unavailable")?};
                            if final_journal.state==RegularFileJournalStateV1::RegularFileV1Prepared {
                                let old=final_journal.clone();final_journal.state=RegularFileJournalStateV1::RegularFileV1Conflict;
                                if !coordinator.catalog().compare_restore_exact(&id,Some(&old),&final_journal).map_err(|_|"regular state unavailable")? {return Err("regular state unavailable".into())}
                            }
                            read_status(&final_journal,&journal.expected,&id)
                        }.await;
                        result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"regular_state_unavailable".into(),message:"Regular-file recovery is unavailable or needs review.".into()})
                    };
                    #[cfg(not(unix))]
                    let response=ServiceResponse::Unavailable{code:"regular_state_unavailable".into(),message:"Regular-file recovery is unavailable.".into()};
                    let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break}continue;
                }
                // Workspace candidates and their index fence share the same
                // coordinator and bounded registry snapshot as the anchor.
                let workspace_request=match serde_json::from_slice::<ServiceRequest>(&frame) {
                    Ok(req @ ServiceRequest::ListWorkspaceActivity{..}) | Ok(req @ ServiceRequest::WorkspaceActivityFence{..})=>Some(req),_=>None};
                if let Some(workspace_request)=workspace_request {
                    let coordinator=coordinator.lock().await;
                    let result=(|| -> Result<ServiceResponse,String> {
                        let registry=ResourceRegistry::load_bounded(&resource_map,4*1024*1024).map_err(|_|"workspace unavailable")?;
                        let (envelope,resource_id,incarnation,scopes_digest,registry_revision,authority_generation,after,fence,graph_activity)=match workspace_request {
                            ServiceRequest::WorkspaceActivityFence{envelope,resource_id}=>(envelope,resource_id,None,None,None,None,None,true,false),
                            ServiceRequest::ListWorkspaceActivity{envelope,resource_id,incarnation,scopes_digest,registry_revision,authority_generation,after,graph_activity}=>(envelope,resource_id,incarnation,scopes_digest,registry_revision,authority_generation,after,false,graph_activity),
                            _=>return Err("workspace unavailable".into()),
                        };
                        auth_bindings.validate_control_cap(&envelope,"read").map_err(|_|"workspace unavailable")?;
                        if after.is_some() != (incarnation.is_some() && scopes_digest.is_some() && registry_revision.is_some() && authority_generation.is_some()) || (after.is_none() && (incarnation.is_some() || scopes_digest.is_some() || registry_revision.is_some() || authority_generation.is_some())) {return Err("workspace cursor unavailable".into());}
                        let (key,_,anchor)=registered_files_resource_key_in(&resource_id,&envelope.auth,&grants,&registry,&host_root,&authorized_roots,false).map_err(|_|"workspace unavailable")?;
                        if anchor.root_id.is_empty() {return Err("workspace unavailable".into())}
                        let current_authority=auth_bindings.authority_generation.ok_or("workspace unavailable")?;
                        if authority_generation.is_some_and(|v|v!=current_authority) || registry_revision.is_some_and(|v|v!=registry.revision) {return Err("stale workspace cursor".into())}
                        let (incarnation,scopes_digest,rows,next)=coordinator.catalog().workspace_recorded_activity(&key.account_id,&key.workspace_id,incarnation.as_deref(),scopes_digest.as_deref(),after.as_deref(),fence).map_err(|e|e.to_string())?;
                        if fence {return Ok(ServiceResponse::WorkspaceActivityFence{incarnation,scopes_digest,registry_revision:registry.revision,authority_generation:current_authority,root_id:anchor.root_id,workspace_id:key.workspace_id})}
                        let items=rows.into_iter().map(|(position,resource,action,expiry)| {
                            let registration=registry.entries.get(&resource.resource_id).filter(|entry|entry.active && entry.account_id==key.account_id && entry.workspace_id==key.workspace_id && entry.root_id==anchor.root_id && entry.root_path==anchor.root_path && resolve_registered_path(&envelope.auth.actor_id,entry,&host_root,&authorized_roots).is_ok()).cloned();
                            let (locator,graph_witness)=if graph_activity {
                                match openclank_history::operations::workspace_activity_witness(&action,&resource).map_err(|_|"workspace unavailable")? {
                                    Some((locator,witness))=>(Some(locator),Some(witness)),
                                    None=>(None,None),
                                }
                            } else {
                                (openclank_history::operations::cross_origin_action_locator(&action,&resource).map_err(|_|"workspace unavailable")?,None)
                            };
                            let recorded_millis=action.timestamp_millis.map(|v|v.to_string());let state=format!("{:?}",action.state).to_ascii_lowercase();
                            let availability=match expiry.map(|v|v.state) {Some(openclank_history::retention::ExpiryState::Expired)=>"expired",Some(openclank_history::retention::ExpiryState::Expiring)=>"expiring",None=>"available"}.to_owned();
                            Ok(openclank_history::protocol::CrossOriginCandidate{position,resource,action_id:action.action_id,recorded_millis,state,availability,locator,registration,graph_witness})
                        }).collect::<Result<Vec<_>,String>>()?;
                        let page=openclank_history::protocol::WorkspaceActivityPage{incarnation,scopes_digest,registry_revision:registry.revision,authority_generation:current_authority,root_id:anchor.root_id,workspace_id:key.workspace_id,items,next};
                        if serde_json::to_vec(&page).map_err(|_|"workspace unavailable")?.len()>900*1024{return Err("workspace unavailable".into())}
                        Ok(ServiceResponse::WorkspaceActivity(page))
                    })();
                    let response=result.unwrap_or_else(|e|ServiceResponse::Unavailable{code:if e=="stale workspace cursor"{"stale_cursor"}else{"workspace_unavailable"}.into(),message:"Workspace activity unavailable or needs refresh.".into()});
                    drop(coordinator);
                    let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break}continue;
                }
                // Cross-origin uses one bounded no-follow registry snapshot under
                // the same coordinator gate as index and binding mutations.
                let origin_request=match serde_json::from_slice::<ServiceRequest>(&frame) {
                    Ok(req @ ServiceRequest::ListCrossOriginChanges{..}) | Ok(req @ ServiceRequest::CrossOriginFence{..})=>Some(req),_=>None};
                if let Some(origin_request)=origin_request {
                    let coordinator=coordinator.lock().await;
                    let result=(|| -> Result<ServiceResponse,String> {
                        let registry=ResourceRegistry::load_bounded(&resource_map,4*1024*1024).map_err(|_|"origin unavailable")?;
                        let (envelope,resource_id,action_id,incarnation,origin_revision,registry_revision,authority_generation,after,fence)=match origin_request {
                            ServiceRequest::CrossOriginFence{envelope,resource_id,origin_action_id}=>(envelope,resource_id,origin_action_id,None,None,None,None,None,true),
                            ServiceRequest::ListCrossOriginChanges{envelope,resource_id,origin_action_id,incarnation,origin_revision,registry_revision,authority_generation,after}=>(envelope,resource_id,origin_action_id,incarnation,origin_revision,registry_revision,authority_generation,after,false),
                            _=>return Err("origin unavailable".into()),
                        };
                        auth_bindings.validate_control_cap(&envelope,"read").map_err(|_|"origin unavailable")?;
                        if after.is_some() != (incarnation.is_some() && origin_revision.is_some() && registry_revision.is_some() && authority_generation.is_some())
                            || (after.is_none() && (incarnation.is_some() || origin_revision.is_some() || registry_revision.is_some() || authority_generation.is_some())) {return Err("origin cursor unavailable".into());}
                        let (key,_,_)=registered_files_resource_key_in(&resource_id,&envelope.auth,&grants,&registry,&host_root,&authorized_roots,true).map_err(|_|"origin unavailable")?;
                        // Preserve the legacy Mimo anchor unless this exact, already
                        // authorized resource/action proves an actual Files capture.
                        let files_source = coordinator.catalog().get_action(&action_id).map_err(|_|"origin unavailable")?
                            .filter(|action| action.before_resources.len()==1
                                && action.before_resources[0].resource_key.provider=="odysseus-files"
                                && openclank_history::operations::cross_origin_action_locator(action,&key).ok().flatten().is_some());
                        let source = if files_source.is_some() { key } else {
                            openclank_history::catalog::ResourceKey {provider:"mimo-filesystem".into(),..key}
                        };
                        let current_authority=auth_bindings.authority_generation.ok_or("origin authority unavailable")?;
                        if authority_generation.is_some_and(|value|value!=current_authority){return Err("stale origin cursor".into());}
                        if fence {let (incarnation,origin_revision)=coordinator.catalog().cross_origin_fence(&source.account_id,&source,&action_id).map_err(|_|"origin unavailable")?;
                            return Ok(ServiceResponse::CrossOriginFence{incarnation,origin_revision,registry_revision:registry.revision,authority_generation:current_authority});}
                        if registry_revision.is_some_and(|revision|revision!=registry.revision){return Err("stale origin cursor".into());}
                        let page=coordinator.catalog().cross_origin_changes(&source.account_id,&source,&action_id,incarnation.as_deref(),origin_revision,after.as_deref()).map_err(|error|error.to_string())?;
                        let items=page.items.into_iter().map(|(position,resource,action,expiry)| {
                            let registration=registry.entries.get(&resource.resource_id).cloned();
                            let locator=openclank_history::operations::cross_origin_action_locator(&action,&resource).map_err(|_|"origin unavailable")?;
                            let recorded_millis=action.timestamp_millis.map(|value|value.to_string());
                            let state=format!("{:?}",action.state).to_ascii_lowercase();
                            let availability=match expiry.map(|value|value.state) {Some(openclank_history::retention::ExpiryState::Expired)=>"expired",Some(openclank_history::retention::ExpiryState::Expiring)=>"expiring",None=>"available"}.to_owned();
                            Ok(openclank_history::protocol::CrossOriginCandidate {position,resource,action_id:action.action_id,recorded_millis,state,availability,locator,registration,graph_witness:None})
                        }).collect::<Result<Vec<_>,String>>()?;
                        let page=openclank_history::protocol::CrossOriginCandidatePage {incarnation:page.incarnation,origin_revision:page.revision,registry_revision:registry.revision,authority_generation:current_authority,items,next:page.next};
                        if serde_json::to_vec(&page).map_err(|_|"origin unavailable")?.len()>900*1024{return Err("origin page unavailable".into());}
                        Ok(ServiceResponse::CrossOriginChanges(page))
                    })();
                    let response=result.unwrap_or_else(|error|ServiceResponse::Unavailable{code:if error=="stale origin cursor"{"stale_cursor"}else{"origin_unavailable"}.into(),message:"Origin unavailable or needs refresh.".into()});
                    drop(coordinator);
                    let encoded=match serde_json::to_string(&response){Ok(value)=>value,Err(_)=>break};
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}
                    continue;
                }
                let restricted = {
                    let coordinator = coordinator.lock().await;
                    restricted_resource_frame(&frame, &resource_map, &authorized_roots, &auth_bindings, &coordinator)
                };
                if restricted {
                    let response = ServiceResponse::Error { code: "provider root is unauthorized for this actor".into() };
                    let encoded = serde_json::to_string(&response).expect("error response serializes");
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err() { break; }
                    continue;
                }
                // A Files-only root role claims the existing action exactly once. The
                // state transition is durable; a lost slot is never re-armed.
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::ClaimFilesWorktree { envelope, root_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let _admission=staging_admission.lock().await;let coordinator=coordinator.lock().await;let mut uploads=staged_uploads.lock().await;
                    let result=(|| -> Result<ServiceResponse,String> {
                        let binding=auth_bindings.validate_files_worktree(&envelope,&root_id).map_err(|_|"worktree unavailable")?;
                        let action=coordinator.catalog().get_action(&envelope.action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        if !matches!(action.operation.as_str(),"source_worktree_create_v2"|"source_worktree_redo_v2") || action.state!=ActionState::Applied || action.actor_id!=binding.actor_id || action.actor_account_id!=binding.account_id {return Err("worktree unavailable".into());}
                        let (key,_,entry)=registered_files_resource_key(&action.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        if key!=action.resource_key || entry.root_id!=root_id {return Err("worktree unavailable".into());}
                        let marker=action.coverage.as_ref().and_then(|c|c.metadata.as_ref()).and_then(|m|m.get("source_worktree_tree_v2")).ok_or("worktree unavailable")?;
                        if marker.get("version")!=Some(&serde_json::json!(2)) || marker.get("allowed_after_format").and_then(|v|v.as_str())!=Some("indexed_tree_v2") {return Err("worktree unavailable".into());}
                        let live=action.live.as_ref().filter(|r|r.action_id==action.action_id && r.status==openclank_history::catalog::LiveStatus::Committed && r.committed_resources.len()==1).ok_or("worktree unavailable")?;
                        let proof=&live.committed_resources[0];let fingerprint=live.fingerprint.as_ref().ok_or("worktree unavailable")?.clone();
                        let len=proof.coverage.byte_len.ok_or("worktree unavailable")?;let after=proof.coverage.metadata.as_ref().and_then(|m|m.get("source_worktree_tree_v2_after")).ok_or("worktree unavailable")?;
                        if proof.resource_key!=key || proof.fingerprint!=fingerprint || proof.coverage.content_digest.as_deref()!=Some(fingerprint.as_str()) || !staged_fingerprint_valid(&fingerprint,len) || len>512*1024*1024+4*1024*1024+12 || len>staging_quota
                            || after.get("journal_operation_id")!=marker.get("journal_operation_id") || after.get("container_length").and_then(|v|v.as_u64())!=Some(len) || uploads.len()>=max_active_stages {return Err("worktree unavailable".into());}
                        let mut random=[0u8;24];use std::io::Read as _;std::fs::File::open("/dev/urandom").and_then(|mut f|f.read_exact(&mut random)).map_err(|_|"worktree unavailable")?;
                        let id=format!("odysseus-worktree-{}",random.iter().map(|v|format!("{v:02x}")).collect::<String>());
                        if uploads.contains_key(&id) {return Err("worktree unavailable".into());}
                        coordinator.claim_files_worktree_after(&action.action_id).map_err(|_|"worktree unavailable")?;
                        uploads.insert(id.clone(),StagedUpload{checkpoint_slot_not_started:true,stage_file_ready:false,read_file:None,read_version_id:None,read_digest:None,worktree_completion_issued:false,actor_id:binding.actor_id,account_id:binding.account_id,action_id:action.action_id,content_length:len,written:0,fingerprint:fingerprint.clone(),finished:false,last_activity_millis:registry_timestamp(),path:staged_upload_path(&staging_root,&id),io_lock:std::sync::Arc::new(tokio::sync::Mutex::new(())),io_active_count:std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0))});
                        Ok(ServiceResponse::FilesWorktreeSlot{upload_id:id,fingerprint,content_length:len})
                    })();
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree capture unavailable".into()});
                    drop(uploads);drop(coordinator);drop(_admission);let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::CompleteFilesWorktree { envelope, root_id, upload_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let result=async {
                        let binding=auth_bindings.validate_files_worktree(&envelope,&root_id).map_err(|_|"worktree unavailable")?;
                        if !upload_id.starts_with("odysseus-worktree-") || upload_id.starts_with("odysseus-worktree-read-") {return Err("worktree unavailable".to_string());}
                        let upload={let mut uploads=staged_uploads.lock().await;let slot=uploads.get_mut(&upload_id).ok_or("worktree unavailable")?;
                            if slot.actor_id!=binding.actor_id || slot.account_id!=binding.account_id || slot.action_id!=envelope.action_id || !slot.finished || !slot.stage_file_ready || slot.worktree_completion_issued {return Err("worktree unavailable".to_string());}
                            slot.io_active_count.fetch_add(1,std::sync::atomic::Ordering::AcqRel);slot.worktree_completion_issued=true;slot.last_activity_millis=registry_timestamp();slot.clone()};
                        let _permit=StagedIoPermit{active:upload.io_active_count.clone()};let _guard=upload.io_lock.lock().await;
                        let coordinator=coordinator.lock().await;let action=coordinator.catalog().get_action(&envelope.action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        let (key,_,entry)=registered_files_resource_key(&action.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        if key!=action.resource_key || entry.root_id!=root_id || action.actor_id!=binding.actor_id || action.actor_account_id!=binding.account_id || action.state!=ActionState::CapturingAfter {return Err("worktree unavailable".to_string());}
                        #[cfg(target_os="linux")]
                        {use std::os::unix::fs::OpenOptionsExt;let mut options=std::fs::OpenOptions::new();options.read(true).custom_flags(libc::O_NOFOLLOW);let file=options.open(&upload.path).map_err(|_|"worktree unavailable")?;
                            let result=coordinator.complete_files_worktree_stage(&action.action_id,file).await.map_err(|_|"worktree unavailable")?;Ok(ServiceResponse::Action(result))}
                        #[cfg(not(target_os="linux"))] {Err("worktree unavailable".to_string())}
                    }.await;
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree capture unavailable".into()});
                    let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::LookupFilesWorktreeVersion { envelope, root_id, operation_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let coordinator=coordinator.lock().await;
                    let result=(|| -> Result<ServiceResponse,String> {
                        let binding=auth_bindings.validate_files_worktree(&envelope,&root_id).map_err(|_|"worktree unavailable")?;
                        let action=coordinator.catalog().get_action(&envelope.action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        let (key,_,entry)=registered_files_resource_key(&action.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        let marker=action.coverage.as_ref().and_then(|v|v.metadata.as_ref()).and_then(|v|v.get("source_worktree_tree_v2")).ok_or("worktree unavailable")?;
                        if action.state!=ActionState::Complete || action.actor_account_id!=binding.account_id || key!=action.resource_key || entry.root_id!=root_id
                            || marker.get("journal_operation_id").and_then(serde_json::Value::as_str)!=Some(operation_id.as_str())
                            || coordinator.catalog().expiry(&action.action_id).map_err(|_|"worktree unavailable")?.is_some() {return Err("worktree unavailable".into())}
                        let version=if action.operation=="source_worktree_inverse_v2" {action.before.as_ref()} else if matches!(action.operation.as_str(),"source_worktree_create_v2"|"source_worktree_redo_v2") {action.after.as_ref()} else {None}.ok_or("worktree unavailable")?;
                        if version.version_id.is_empty() || version.version_id.len()>256 {return Err("worktree unavailable".into())}
                        Ok(ServiceResponse::FilesWorktreeVersion{version_id:version.version_id.clone()})
                    })();
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree version unavailable".into()});
                    drop(coordinator);let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::FinalizeFilesWorktree { envelope, root_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let coordinator=coordinator.lock().await;
                    let result=(|| -> Result<ServiceResponse,String> {
                        let binding=auth_bindings.validate_files_worktree(&envelope,&root_id).map_err(|_|"worktree unavailable")?;
                        let action=coordinator.catalog().get_action(&envelope.action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        let (key,_,entry)=registered_files_resource_key(&action.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        if !matches!(action.operation.as_str(),"source_worktree_create_v2"|"source_worktree_redo_v2") || key!=action.resource_key || entry.root_id!=root_id || action.actor_id!=binding.actor_id || action.actor_account_id!=binding.account_id {return Err("worktree unavailable".into());}
                        match action.state {
                            ActionState::AfterDurable => Ok(ServiceResponse::Action(coordinator.complete(&action.action_id).map_err(|_|"worktree unavailable")?)),
                            ActionState::Complete => Ok(ServiceResponse::Action(action)),
                            _ => Err("worktree unavailable".into()),
                        }
                    })();
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree capture unavailable".into()});
                    drop(coordinator);let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::FinalizeFilesWorktreeInverse { envelope, root_id, journal_operation_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let coordinator=coordinator.lock().await;
                    let result=async {
                        let binding=auth_bindings.validate_files_worktree(&envelope,&root_id).map_err(|_|"worktree unavailable")?;
                        let action=coordinator.catalog().get_action(&envelope.action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        let (key,_,entry)=registered_files_resource_key(&action.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        if action.operation!="source_worktree_inverse_v2" || action.state!=ActionState::Applied || action.actor_kind!="human"
                            || action.actor_id!=binding.actor_id || action.actor_account_id!=binding.account_id || key!=action.resource_key || entry.root_id!=root_id
                            || action.coverage.as_ref().and_then(|v|v.metadata.as_ref()).and_then(|v|v.pointer("/source_worktree_tree_v2/journal_operation_id")).and_then(serde_json::Value::as_str)!=Some(journal_operation_id.as_str()) {return Err("worktree unavailable".to_string());}
                        coordinator.capture_inverse_absence(&action.action_id,&journal_operation_id).await.map_err(|_|"worktree unavailable")?;
                        let complete=coordinator.complete(&action.action_id).map_err(|_|"worktree unavailable")?;
                        Ok::<_,String>(ServiceResponse::Action(complete))
                    }.await;
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree inverse unavailable".into()});
                    drop(coordinator);let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::PrepareFilesWorktreeInverse { envelope, root_id, original_action_id, original_version_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let _admission=staging_admission.lock().await; let coordinator=coordinator.lock().await;
                    let result=async {
                        let control=ControlEnvelope{protocol_version:envelope.protocol_version,auth:envelope.auth.clone(),action_id:envelope.request.action_id.clone()};
                        let binding=auth_bindings.validate_files_worktree(&control,&root_id).map_err(|_|"worktree unavailable")?;
                        protocol_validate(&envelope,&binding.account_id,&binding.token).map_err(|_|"worktree unavailable")?;
                        if original_action_id.is_empty() || original_action_id.len()>256 || original_version_id.is_empty() || original_version_id.len()>256
                            || original_action_id==envelope.request.action_id { return Err("worktree unavailable".to_string()); }
                        let original=coordinator.catalog().get_action(&original_action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        let inverse=&envelope.request;
                        let (original_key,_,original_entry)=registered_files_resource_key(&original.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        let (inverse_key,_,inverse_entry)=registered_files_resource_key(&inverse.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        if original_key!=original.resource_key || inverse_key!=inverse.resource_key || original_key!=inverse_key
                            || original_entry.root_id!=root_id || inverse_entry.root_id!=root_id || original.actor_account_id!=binding.account_id
                            || inverse.actor_account_id!=binding.account_id || inverse.actor_id!=binding.actor_id || inverse.actor_kind!="human" || inverse.operation!="source_worktree_inverse_v2"
                            || original.state!=ActionState::Complete || original.after.as_ref().map(|v|v.version_id.as_str())!=Some(original_version_id.as_str())
                            || coordinator.catalog().expiry(&original_action_id).map_err(|_|"worktree unavailable")?.is_some() {return Err("worktree unavailable".to_string());}
                        let after=original.after_resources.first().filter(|_|original.after_resources.len()==1).ok_or("worktree unavailable")?;
                        let length=after.coverage.byte_len.ok_or("worktree unavailable")?;
                        let mut trusted_request=envelope.request.clone();
                        trusted_request.physical_lease_keys=trusted_registry_aliases(&trusted_request,&resource_map);
                        match begin(coordinator.catalog(),trusted_request).map_err(|_|"worktree unavailable")? {
                            BeginResult::New(_) => {}, BeginResult::Existing(_) => return Err("worktree unavailable".to_string()),
                        }
                        if length>512*1024*1024+4*1024*1024+12 || length>staging_quota || staged_uploads.lock().await.len()>=max_active_stages {return Err("worktree unavailable".to_string());}
                        staging_budget_error(&coordinator,&staging_root,length,staged_bytes.load(std::sync::atomic::Ordering::Acquire))?;
                        let mut random=[0u8;24];use std::io::Read as _;std::fs::File::open("/dev/urandom").and_then(|mut f|f.read_exact(&mut random)).map_err(|_|"worktree unavailable")?;
                        let stage_id=format!("odysseus-worktree-read-{}",random.iter().map(|v|format!("{v:02x}")).collect::<String>());
                        let path=staged_upload_path(&staging_root,&stage_id);
                        #[cfg(target_os="linux")]
                        {
                            use std::os::unix::fs::OpenOptionsExt;
                            let mut options=std::fs::OpenOptions::new();options.read(true).write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW);
                            let mut slots=staged_uploads.lock().await;
                            let file=options.open(&path).map_err(|_|"worktree unavailable")?;
                            let capture=match file.try_clone(){Ok(f)=>f,Err(_)=>{let _=std::fs::remove_file(&path);return Err("worktree unavailable".to_string())}};
                            if staged_bytes.fetch_update(std::sync::atomic::Ordering::AcqRel,std::sync::atomic::Ordering::Acquire,|used|used.checked_add(length).filter(|v|*v<=staging_quota)).is_err(){let _=std::fs::remove_file(&path);return Err("worktree unavailable".to_string());}
                            let active=std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(1));
                            let slot=StagedUpload{checkpoint_slot_not_started:false,stage_file_ready:true,read_file:Some(std::sync::Arc::new(file)),read_version_id:Some(original_version_id.clone()),read_digest:None,worktree_completion_issued:false,actor_id:binding.actor_id,account_id:binding.account_id,action_id:envelope.request.action_id.clone(),content_length:length,written:0,fingerprint:after.fingerprint.clone(),finished:false,last_activity_millis:registry_timestamp(),path,io_lock:std::sync::Arc::new(tokio::sync::Mutex::new(())),io_active_count:active.clone()};
                            slots.insert(stage_id.clone(),slot); drop(slots);
                            let permit=StagedIoPermit{active};
                            let captured=coordinator.prepare_files_worktree_inverse(&original_action_id,&original_version_id,&envelope.request.action_id,capture).await;
                            drop(permit);
                            if let Some(slot)=staged_uploads.lock().await.remove(&stage_id){remove_staged_upload(&slot,&staged_bytes);}
                            captured.map(ServiceResponse::Action).map_err(|_|"worktree unavailable".to_string())
                        }
                        #[cfg(not(target_os="linux"))] {Err("worktree unavailable".to_string())}
                    }.await;
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree inverse unavailable".into()});
                    drop(coordinator);drop(_admission);let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::PrepareFilesWorktreeRedo { envelope, root_id, inverse_action_id, inverse_version_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let _admission=staging_admission.lock().await; let coordinator=coordinator.lock().await;
                    let result=async {
                        let control=ControlEnvelope{protocol_version:envelope.protocol_version,auth:envelope.auth.clone(),action_id:envelope.request.action_id.clone()};
                        let binding=auth_bindings.validate_files_worktree(&control,&root_id).map_err(|_|"worktree unavailable")?;
                        protocol_validate(&envelope,&binding.account_id,&binding.token).map_err(|_|"worktree unavailable")?;
                        if inverse_action_id.is_empty() || inverse_action_id.len()>256 || inverse_version_id.is_empty() || inverse_version_id.len()>256
                            || inverse_action_id==envelope.request.action_id { return Err("worktree unavailable".to_string()); }
                        let source=coordinator.catalog().get_action(&inverse_action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        let redo=&envelope.request;
                        let (source_key,_,source_entry)=registered_files_resource_key(&source.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        let (redo_key,_,redo_entry)=registered_files_resource_key(&redo.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        if source_key!=source.resource_key || redo_key!=redo.resource_key || source_key!=redo_key
                            || source_entry.root_id!=root_id || redo_entry.root_id!=root_id || source.actor_account_id!=binding.account_id
                            || redo.actor_account_id!=binding.account_id || redo.actor_id!=binding.actor_id || redo.actor_kind!="human" || redo.operation!="source_worktree_redo_v2"
                            || source.operation!="source_worktree_inverse_v2" || source.state!=ActionState::Complete || source.before.as_ref().map(|v|v.version_id.as_str())!=Some(inverse_version_id.as_str())
                            || coordinator.catalog().expiry(&inverse_action_id).map_err(|_|"worktree unavailable")?.is_some() {return Err("worktree unavailable".to_string());}
                        let after=source.before_resources.first().filter(|_|source.before_resources.len()==1).ok_or("worktree unavailable")?;
                        let length=after.coverage.byte_len.ok_or("worktree unavailable")?;
                        let mut trusted_request=envelope.request.clone();
                        trusted_request.physical_lease_keys=trusted_registry_aliases(&trusted_request,&resource_map);
                        match begin(coordinator.catalog(),trusted_request).map_err(|_|"worktree unavailable")? {
                            BeginResult::New(_) => {}, BeginResult::Existing(_) => return Err("worktree unavailable".to_string()),
                        }
                        if length>512*1024*1024+4*1024*1024+12 || length>staging_quota || staged_uploads.lock().await.len()>=max_active_stages {return Err("worktree unavailable".to_string());}
                        staging_budget_error(&coordinator,&staging_root,length,staged_bytes.load(std::sync::atomic::Ordering::Acquire))?;
                        let mut random=[0u8;24];use std::io::Read as _;std::fs::File::open("/dev/urandom").and_then(|mut f|f.read_exact(&mut random)).map_err(|_|"worktree unavailable")?;
                        let stage_id=format!("odysseus-worktree-read-{}",random.iter().map(|v|format!("{v:02x}")).collect::<String>());
                        let path=staged_upload_path(&staging_root,&stage_id);
                        #[cfg(target_os="linux")]
                        {
                            use std::os::unix::fs::OpenOptionsExt;
                            let mut options=std::fs::OpenOptions::new();options.read(true).write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW);
                            let mut slots=staged_uploads.lock().await;
                            let file=options.open(&path).map_err(|_|"worktree unavailable")?;
                            let capture=match file.try_clone(){Ok(f)=>f,Err(_)=>{let _=std::fs::remove_file(&path);return Err("worktree unavailable".to_string())}};
                            if staged_bytes.fetch_update(std::sync::atomic::Ordering::AcqRel,std::sync::atomic::Ordering::Acquire,|used|used.checked_add(length).filter(|v|*v<=staging_quota)).is_err(){let _=std::fs::remove_file(&path);return Err("worktree unavailable".to_string());}
                            let active=std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(1));
                            let slot=StagedUpload{checkpoint_slot_not_started:false,stage_file_ready:true,read_file:Some(std::sync::Arc::new(file)),read_version_id:Some(inverse_version_id.clone()),read_digest:None,worktree_completion_issued:false,actor_id:binding.actor_id,account_id:binding.account_id,action_id:envelope.request.action_id.clone(),content_length:length,written:0,fingerprint:after.fingerprint.clone(),finished:false,last_activity_millis:registry_timestamp(),path,io_lock:std::sync::Arc::new(tokio::sync::Mutex::new(())),io_active_count:active.clone()};
                            slots.insert(stage_id.clone(),slot); drop(slots);
                            let permit=StagedIoPermit{active};
                            let captured=async {
                                coordinator.capture_batch_before(&envelope.request.action_id,vec![BatchCaptureInput {resource_key:redo.resource_key.clone(),old_locator:None,new_locator:redo.destination_locator.clone(),expected_revision:redo.expected_revision.clone(),existence:openclank_history::catalog::ResourceExistence::Absent,resource_type:openclank_history::catalog::ResourceType::Directory,metadata:openclank_history::catalog::ResourceMetadata::default(),content:None,fingerprint:"missing".into(),coverage:openclank_history::catalog::CaptureManifest::default()}]).await?;
                                coordinator.prepare_files_worktree_redo_after(&envelope.request.action_id,&inverse_action_id,&inverse_version_id,capture).await
                            }.await;
                            drop(permit);
                            if captured.is_err(){if let Some(slot)=staged_uploads.lock().await.remove(&stage_id){remove_staged_upload(&slot,&staged_bytes);}let _=coordinator.abort(&envelope.request.action_id);return Err("worktree unavailable".to_string())}
                            let action=coordinator.catalog().get_action(&envelope.request.action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                            let fingerprint=after.fingerprint.clone();coordinator.validate_files_worktree_redo_read(&action.action_id,&inverse_version_id,length,&fingerprint).map_err(|_|"worktree unavailable")?;
                            {let mut slots=staged_uploads.lock().await;let slot=slots.get_mut(&stage_id).ok_or("worktree unavailable")?;slot.finished=true;slot.read_digest=Some(sha2::Sha256::new());}
                            Ok(ServiceResponse::FilesWorktreeRedoPrepared{action,read_id:stage_id,content_length:length,fingerprint})
                        }
                        #[cfg(not(target_os="linux"))] {Err("worktree unavailable".to_string())}
                    }.await;
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree inverse unavailable".into()});
                    drop(coordinator);drop(_admission);let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::BeginFilesWorktreeRead { envelope, root_id, version_id }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let _admission=staging_admission.lock().await;let coordinator=coordinator.lock().await;
                    let result=async {
                        let binding=auth_bindings.validate_files_worktree(&envelope,&root_id).map_err(|_|"worktree unavailable")?;
                        let action=coordinator.catalog().get_action(&envelope.action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        let (key,_,entry)=registered_files_resource_key(&action.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        if action.actor_id!=binding.actor_id || action.actor_account_id!=binding.account_id || key!=action.resource_key || entry.root_id!=root_id || coordinator.catalog().expiry(&action.action_id).map_err(|_|"worktree unavailable")?.is_some() {return Err("worktree unavailable".to_string());}
                        let (length,fingerprint)=worktree_read_projection(&action,&version_id).ok_or("worktree unavailable")?;
                        if length>512*1024*1024+4*1024*1024+12 || length>staging_quota || staged_uploads.lock().await.len()>=max_active_stages {return Err("worktree unavailable".to_string());}
                        staging_budget_error(&coordinator,&staging_root,length,staged_bytes.load(std::sync::atomic::Ordering::Acquire))?;
                        let mut random=[0u8;24];use std::io::Read as _;std::fs::File::open("/dev/urandom").and_then(|mut f|f.read_exact(&mut random)).map_err(|_|"worktree unavailable")?;
                        let read_id=format!("odysseus-worktree-read-{}",random.iter().map(|v|format!("{v:02x}")).collect::<String>());let path=staged_upload_path(&staging_root,&read_id);
                        #[cfg(target_os="linux")]
                        {use std::os::unix::fs::OpenOptionsExt;let mut options=std::fs::OpenOptions::new();options.read(true).write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW);
                            let file=options.open(&path).map_err(|_|"worktree unavailable")?;
                            let target=match file.try_clone(){Ok(f)=>f,Err(_)=>{let _=std::fs::remove_file(&path);return Err("worktree unavailable".to_string())}};
                            if staged_bytes.fetch_update(std::sync::atomic::Ordering::AcqRel,std::sync::atomic::Ordering::Acquire,|used|used.checked_add(length).filter(|v|*v<=staging_quota)).is_err(){let _=std::fs::remove_file(&path);return Err("worktree unavailable".to_string());}
                            if coordinator.read_files_worktree_stage(&action.action_id,&binding.account_id,&version_id,target).await.is_err(){staged_bytes.fetch_sub(length,std::sync::atomic::Ordering::AcqRel);let _=std::fs::remove_file(&path);return Err("worktree unavailable".to_string());}
                            let slot=StagedUpload{checkpoint_slot_not_started:false,stage_file_ready:true,read_file:Some(std::sync::Arc::new(file)),read_version_id:Some(version_id),read_digest:Some(Sha256::new()),worktree_completion_issued:false,actor_id:binding.actor_id,account_id:binding.account_id,action_id:action.action_id.clone(),content_length:length,written:0,fingerprint:fingerprint.to_owned(),finished:true,last_activity_millis:registry_timestamp(),path,io_lock:std::sync::Arc::new(tokio::sync::Mutex::new(())),io_active_count:std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0))};
                            let fingerprint=slot.fingerprint.clone();staged_uploads.lock().await.insert(read_id.clone(),slot);Ok(ServiceResponse::FilesWorktreeRead{read_id,content_length:length,fingerprint})}
                        #[cfg(not(target_os="linux"))] {Err("worktree unavailable".to_string())}
                    }.await;
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree read unavailable".into()});
                    drop(coordinator);drop(_admission);let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                #[cfg(target_os = "linux")]
                if let Ok(ServiceRequest::FilesWorktreeReadChunk { envelope, root_id, read_id, offset }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let result=async {
                        let binding=auth_bindings.validate_files_worktree(&envelope,&root_id).map_err(|_|"worktree unavailable")?;
                        if !read_id.starts_with("odysseus-worktree-read-"){return Err("worktree unavailable".to_string());}
                        let upload={let uploads=staged_uploads.lock().await;let slot=uploads.get(&read_id).ok_or("worktree unavailable")?;
                            if slot.actor_id!=binding.actor_id || slot.account_id!=binding.account_id || slot.action_id!=envelope.action_id || !slot.finished || !slot.stage_file_ready || slot.written!=offset {return Err("worktree unavailable".to_string());}
                            slot.io_active_count.fetch_add(1,std::sync::atomic::Ordering::AcqRel);slot.clone()};
                        let _permit=StagedIoPermit{active:upload.io_active_count.clone()};let _guard=upload.io_lock.lock().await;
                        let coordinator=coordinator.lock().await;let action=coordinator.catalog().get_action(&envelope.action_id).map_err(|_|"worktree unavailable")?.ok_or("worktree unavailable")?;
                        let (key,_,entry)=registered_files_resource_key(&action.resource_key.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,false).map_err(|_|"worktree unavailable")?;
                        let version=upload.read_version_id.as_deref().ok_or("worktree unavailable")?;
                        let readable=if action.operation=="source_worktree_redo_v2" {coordinator.validate_files_worktree_redo_read(&action.action_id,version,upload.content_length,&upload.fingerprint).is_ok()} else {worktree_read_projection(&action,version).is_some()};
                        if key!=action.resource_key || entry.root_id!=root_id || action.actor_id!=binding.actor_id || action.actor_account_id!=binding.account_id || coordinator.catalog().expiry(&action.action_id).map_err(|_|"worktree unavailable")?.is_some() || !readable {return Err("worktree unavailable".to_string());}
                        #[cfg(target_os="linux")]
                        {use std::os::unix::fs::FileExt;let file=upload.read_file.as_ref().ok_or("worktree unavailable")?;let len=((upload.content_length-offset).min(128*1024)) as usize;let mut bytes=vec![0u8;len];let mut at=0usize;while at<len {let n=file.read_at(&mut bytes[at..],offset+at as u64).map_err(|_|"worktree unavailable")?;if n==0{return Err("worktree unavailable".to_string());}at+=n;}
                            let next=offset+len as u64;let eof=next==upload.content_length;let mut slots=staged_uploads.lock().await;let current=slots.get_mut(&read_id).filter(|s|std::sync::Arc::ptr_eq(&s.io_lock,&upload.io_lock) && s.written==offset).ok_or("worktree unavailable")?;
                            let digest=current.read_digest.as_mut().ok_or("worktree unavailable")?;digest.update(&bytes);
                            if eof && current.fingerprint!=format!("sha256:{:x}:{}",digest.clone().finalize(),next) {let removed=slots.remove(&read_id).ok_or("worktree unavailable")?;remove_staged_upload(&removed,&staged_bytes);return Err("worktree unavailable".to_string());}
                            current.written=next;current.last_activity_millis=registry_timestamp();
                            if eof {let removed=slots.remove(&read_id).ok_or("worktree unavailable")?;remove_staged_upload(&removed,&staged_bytes);}
                            Ok(ServiceResponse::Chunk{offset,content:Some(bytes),eof})}
                        #[cfg(not(target_os="linux"))] {Err("worktree unavailable".to_string())}
                    }.await;
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"worktree_unavailable".into(),message:"Worktree read unavailable".into()});
                    let encoded=match serde_json::to_string(&response){Ok(v)=>v,Err(_)=>break};if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err(){break;}continue;
                }
                if let Ok(ServiceRequest::FinalizeSelectedCheckpoint { envelope, request_id, workspace_id, root_id, items, staged, started_millis, finished_millis }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let response = finalize_checkpoint_fast(envelope,request_id,workspace_id,root_id,items,staged,started_millis,finished_millis,
                        &auth_bindings,&coordinator,&staged_uploads,&staged_bytes,authority_file.as_deref(),&grants,&resource_map,&host_root,&authorized_roots).await;
                    let encoded = match serde_json::to_string(&response) { Ok(value)=>value, Err(_)=>break };
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err() { break; }
                    continue;
                }
                if let Ok(request) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    if let Some(response) = handle_staging_fast(
                        request,
                        &auth_bindings,
                        &coordinator,
                        &staging_root,
                        staging_quota,
                        staging_ttl_millis,
                        max_active_stages,
                        &staged_uploads,
                        &staged_bytes,
                        &staging_admission,
                        &grants,
                    )
                    .await
                    {
                        let encoded = match serde_json::to_string(&response) {
                            Ok(value) => value,
                            Err(_) => break,
                        };
                        if write.write_all(encoded.as_bytes()).await.is_err()
                            || write.write_all(b"\n").await.is_err()
                        {
                            break;
                        }
                        continue;
                    }
                }
                if let Ok(ServiceRequest::FailSelectedCheckpoint { envelope, request_id, workspace_id, root_id, items }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let admission=staging_admission.lock().await;
                    let coordinator=coordinator.lock().await;
                    let mut uploads=staged_uploads.lock().await;
                    let result=(|| -> Result<ServiceResponse,String> {
                        let binding=auth_bindings.validate_checkpoint(&envelope,&root_id).map_err(|_| "checkpoint unavailable")?;
                        if request_id.len()<16 || request_id.len()>80 || !request_id.bytes().all(|c|c.is_ascii_alphanumeric() || c==b'-' || c==b'_') {return Err("checkpoint unavailable".into());}
                        let action=format!("checkpoint-{:x}",Sha256::digest(format!("{}\0{}\0{}",binding.account_id,binding.actor_id,request_id).as_bytes()));
                        if envelope.action_id!=action {return Err("checkpoint unavailable".into());}
                        let original=coordinator.catalog().get_action(&action).map_err(|_| "checkpoint unavailable")?.ok_or("checkpoint unavailable")?;
                        let meta=original.coverage.as_ref().and_then(|c|c.metadata.as_ref()).ok_or("checkpoint unavailable")?;
                        if original.actor_account_id!=binding.account_id || original.actor_id!=binding.actor_id
                            || meta.get("root_id").and_then(|v|v.as_str())!=Some(root_id.as_str())
                            || meta.get("workspace_id").and_then(|v|v.as_str())!=Some(workspace_id.as_str())
                            || meta.get("items")!=Some(&serde_json::to_value(&items).map_err(|_| "checkpoint unavailable")?) {return Err("checkpoint unavailable".into());}
                        let slots:Vec<_>=uploads.iter().filter(|(id,slot)|id.starts_with("odysseus-checkpoint-") && slot.action_id==action).collect();
                        if slots.len()!=items.len() || slots.iter().any(|(_,slot)|slot.actor_id!=binding.actor_id
                            || slot.account_id!=binding.account_id || !slot.checkpoint_slot_not_started || slot.stage_file_ready
                            || !staged_fingerprint_valid(&slot.fingerprint,slot.content_length)) {
                            return Err("checkpoint settlement unavailable".into());
                        }
                        let mut actual:Vec<_>=slots.iter().map(|(_,slot)|slot.fingerprint.clone()).collect();
                        let mut expected:Vec<_>=items.iter().map(|item|item.expected_fingerprint.clone()).collect();
                        actual.sort(); expected.sort();
                        if actual!=expected {return Err("checkpoint settlement unavailable".into());}
                        let ids:Vec<_>=slots.iter().map(|(id,_)|(*id).clone()).collect();
                        let record=coordinator.fail_selected_checkpoint(&action,&original.request_digest).map_err(|_| "checkpoint unavailable")?;
                        for id in ids {uploads.remove(&id);}
                        checkpoint_status(&record,false)
                    })();
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"checkpoint_unavailable".into(),message:"Checkpoint unavailable".into()});
                    drop(uploads); drop(coordinator); drop(admission);
                    let encoded=match serde_json::to_string(&response) {Ok(value)=>value,Err(_)=>break};
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err() {break;}
                    continue;
                }
                if let Ok(ServiceRequest::ClaimSelectedCheckpoint { envelope, request_id, workspace_id, root_id, items }) = serde_json::from_slice::<ServiceRequest>(&frame) {
                    let _admission = staging_admission.lock().await;
                    let coordinator = coordinator.lock().await;
                    let mut uploads = staged_uploads.lock().await;
                    let result = (|| -> Result<ServiceResponse, String> {
                        if !coordinator.catalog().checkpoint_profile().map_err(|_| "checkpoint unavailable")? { return Err("checkpoint unavailable".into()); }
                        let binding=auth_bindings.validate_checkpoint(&envelope,&root_id).map_err(|_| "checkpoint unavailable")?;
                        let (request,total,mut bindings)=checkpoint_action_request(&envelope,&request_id,&workspace_id,&root_id,&items,&binding,&grants,&resource_map,&host_root,&authorized_roots)?;
                        if coordinator.catalog().get_action(&request.action_id).map_err(|_| "checkpoint unavailable")?.is_none()
                            && uploads.len().saturating_add(items.len())>max_active_stages {return Err("checkpoint slots unavailable".into());}
                        let (record,claimed)=coordinator.claim_selected_checkpoint(request,total).map_err(|_| "checkpoint claim unavailable")?;
                        if !claimed {return checkpoint_status(&record,false);}
                        for (entry,item) in bindings.iter_mut().zip(&items) {
                            #[cfg(target_os="linux")]
                            let id={let mut bytes=[0u8;24];let mut random=std::fs::File::open("/dev/urandom").map_err(|_| "checkpoint entropy unavailable")?;
                                std::io::Read::read_exact(&mut random,&mut bytes).map_err(|_| "checkpoint entropy unavailable")?;
                                format!("odysseus-checkpoint-{}",bytes.iter().map(|byte|format!("{byte:02x}")).collect::<String>())};
                            #[cfg(not(target_os="linux"))]
                            let id: String = return Err("checkpoint profile unavailable".into());
                            if uploads.contains_key(&id) {return Err("checkpoint slots unavailable".into());}
                            let size=item.expected_fingerprint.rsplit_once(':').and_then(|(_,value)|value.parse::<u64>().ok()).ok_or("checkpoint size unavailable")?;
                            entry.staged_upload_id=id.clone();
                            uploads.insert(id.clone(),StagedUpload {checkpoint_slot_not_started:true, stage_file_ready:false, read_file:None, read_version_id:None, read_digest:None, worktree_completion_issued:false, actor_id:envelope.auth.actor_id.clone(),account_id:envelope.auth.account_id.clone(),action_id:envelope.action_id.clone(),content_length:size,written:0,fingerprint:item.expected_fingerprint.clone(),finished:false,last_activity_millis:registry_timestamp(),path:staged_upload_path(&staging_root,&id),io_lock:std::sync::Arc::new(tokio::sync::Mutex::new(())),io_active_count:std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0))});
                        }
                        Ok(ServiceResponse::CheckpointClaim {state:"claimed".into(),request_digest:record.request_digest,bindings})
                    })();
                    let response=result.unwrap_or_else(|_|ServiceResponse::Unavailable {code:"checkpoint_unavailable".into(),message:"Checkpoint unavailable".into()});
                    drop(uploads); drop(coordinator); drop(_admission);
                    let encoded=match serde_json::to_string(&response) {Ok(value)=>value,Err(_)=>break};
                    if write.write_all(encoded.as_bytes()).await.is_err() || write.write_all(b"\n").await.is_err() {break;}
                    continue;
                }
                // StageBegin/Chunk/Finish/Abort arms below are retained as a wire-compatibility
                // fallback, but every valid staging request is consumed above and continues
                // before entering this coordinator-locked legacy match.
                let response = {
                    let coordinator = coordinator.lock().await;
                    match serde_json::from_slice::<ServiceRequest>(&frame) {
                        Ok(ServiceRequest::DigestResourceVersion { .. }) => ServiceResponse::Unavailable {code:"digest_unavailable".into(),message:"Recorded version digest unavailable".into()},
                        Ok(ServiceRequest::AuthorityBarrier { .. } | ServiceRequest::AgentAuthorityBarrier { .. }) => ServiceResponse::Error { code: "authority barrier unavailable".into() },
                        Ok(ServiceRequest::LookupFilesWorktreeVersion { .. } | ServiceRequest::FinalizeFilesWorktreeInverse { .. } | ServiceRequest::PrepareFilesWorktreeInverse { .. } | ServiceRequest::PrepareFilesWorktreeRedo { .. } | ServiceRequest::ClaimFilesWorktree { .. } | ServiceRequest::CompleteFilesWorktree { .. } | ServiceRequest::FinalizeFilesWorktree { .. } | ServiceRequest::BeginFilesWorktreeRead { .. } | ServiceRequest::FilesWorktreeReadChunk { .. }) => ServiceResponse::Error { code: "worktree unavailable".into() },
                        Ok(ServiceRequest::Health(envelope)) => {
                            match auth_bindings.validate_control(&envelope) {
                                Ok(_) => ServiceResponse::Health {
                                    protocol_version: 1,
                                    account_id: envelope.auth.account_id.clone(),
                                },
                                Err(error) => ServiceResponse::Error {
                                    code: format!("{error:?}"),
                                },
                            }
                        }
                        Ok(ServiceRequest::FilesWorktreeCapabilities(envelope)) => {
                            match (auth_bindings.validate_control(&envelope),auth_bindings.authority_generation,coordinator.catalog().checkpoint_schema()) {
                                (Ok(binding),Some(generation),Ok(Some(4|5))) if cfg!(target_os="linux")
                                    && binding.actor_id!="*" && !binding.actor_id.starts_with("agent:") && binding.actor_id==envelope.auth.actor_id
                                    && !binding.account_id.is_empty() && binding.account_id!="*" && binding.account_id==envelope.auth.account_id
                                    && binding.owner_accounts.is_empty() && AuthBindings::require(&binding,"read").is_ok() && AuthBindings::require(&binding,"capture").is_ok()
                                    && auth_bindings.credentials.iter().any(|v|v.actor_id==binding.actor_id && v.account_id==binding.account_id && v.token==binding.token && v.checkpoint_root_id.is_none() && v.drain_session_id.is_none()) =>
                                    ServiceResponse::FilesWorktreeCapabilities {process_id:std::process::id(),authority_generation:generation,worktree_capture:true},
                                _ => ServiceResponse::Unavailable {code:"worktree_unavailable".into(),message:"Worktree capture unavailable".into()},
                            }
                        },
                        Ok(ServiceRequest::CheckpointCapabilities(envelope)) => {
                            match (auth_bindings.validate_control(&envelope), auth_bindings.authority_generation, coordinator.catalog().checkpoint_schema()) {
                                (Ok(_), Some(generation), Ok(Some(schema))) => ServiceResponse::CheckpointCapabilities {
                                    schema_version: schema, authority_generation: generation, process_id: std::process::id(), checkpoint_capture: true,
                                },
                                _ => ServiceResponse::Unavailable { code: "checkpoint_unavailable".into(), message: "Checkpoint profile unavailable".into() },
                            }
                        }
                        Ok(ServiceRequest::ClaimSelectedCheckpoint { .. }) => ServiceResponse::Unavailable {code:"checkpoint_unavailable".into(),message:"Checkpoint unavailable".into()},
                        Ok(ServiceRequest::GetSelectedCheckpoint { envelope, request_id }) => {
                            let result = (|| -> Result<ServiceResponse, String> {
                                auth_bindings.validate_control_cap(&envelope,"read").map_err(|_| "checkpoint unavailable")?;
                                if !coordinator.catalog().checkpoint_profile().map_err(|_| "checkpoint unavailable")?
                                    || request_id.len()<16 || request_id.len()>80 || !request_id.bytes().all(|c|c.is_ascii_alphanumeric() || c==b'-' || c==b'_') {return Err("checkpoint unavailable".into());}
                                let action = format!("checkpoint-{:x}",Sha256::digest(format!("{}\0{}\0{}",envelope.auth.account_id,envelope.auth.actor_id,request_id).as_bytes()));
                                if envelope.action_id != action {return Err("checkpoint unavailable".into());}
                                let record = coordinator.catalog().get_action(&action).map_err(|_| "checkpoint unavailable")?.ok_or("checkpoint unavailable")?;
                                if record.actor_account_id != envelope.auth.account_id || record.actor_id != envelope.auth.actor_id
                                    || coordinator.catalog().expiry(&action).map_err(|_| "checkpoint unavailable")?.is_some() {return Err("checkpoint unavailable".into());}
                                let meta = record.coverage.as_ref().and_then(|c|c.metadata.as_ref()).ok_or("checkpoint unavailable")?;
                                let root = meta.get("root_id").and_then(|v|v.as_str()).ok_or("checkpoint unavailable")?;
                                let workspace = meta.get("workspace_id").and_then(|v|v.as_str()).ok_or("checkpoint unavailable")?;
                                for item in &record.modified_resource_ids {
                                    let (key,_,entry)=registered_files_resource_key(&item.resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,true).map_err(|_| "checkpoint unavailable")?;
                                    if key != *item || entry.root_id != root || entry.workspace_id != workspace {return Err("checkpoint unavailable".into());}
                                }
                                checkpoint_status(&record,false)
                            })();
                            result.unwrap_or_else(|_| ServiceResponse::Unavailable { code:"checkpoint_unavailable".into(), message:"Checkpoint unavailable".into() })
                        }
                        Ok(ServiceRequest::FailSelectedCheckpoint { .. }) => ServiceResponse::Unavailable {code:"checkpoint_unavailable".into(),message:"Checkpoint unavailable".into()},
                        Ok(ServiceRequest::FinalizeSelectedCheckpoint { .. }) => ServiceResponse::Unavailable {
                            code:"checkpoint_unavailable".into(), message:"Checkpoint unavailable".into(),
                        },
                        Ok(ServiceRequest::Shutdown(envelope)) => {
                            match auth_bindings.validate_control_cap(&envelope, "admin") {
                                Ok(_) => match coordinator.shutdown_checked().await {
                                    Ok(()) => {
                                        stopping.store(true, Ordering::Release);
                                        ServiceResponse::Accepted
                                    }
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                },
                                Err(error) => ServiceResponse::Error {
                                    code: format!("{error:?}"),
                                },
                            }
                        }
                        Ok(ServiceRequest::Prepare {
                            envelope,
                            content,
                            fingerprint,
                        }) => match auth_bindings
                            .validate_request_cap(&envelope, "capture")
                            .and_then(|_| {
                                if request_resources_allowed(
                                    &envelope.request,
                                    &envelope.auth.actor_id,
                                    &envelope.auth.account_id,
                                    &grants,
                                ) {
                                    Ok(())
                                } else {
                                    Err(openclank_history::protocol::ProtocolError::AccountMismatch)
                                }
                            }) {
                            Ok(()) => {
                                let mut trusted_request = envelope.request.clone();
                                trusted_request.physical_lease_keys =
                                    trusted_registry_aliases(&trusted_request, &resource_map);
                                match begin(coordinator.catalog(), trusted_request) {
                                    Ok(BeginResult::Existing(record))
                                        if !matches!(
                                            record.state,
                                            ActionState::Intent | ActionState::CaptureFailed
                                        ) =>
                                    {
                                        let digest =
                                            capture_input_digest(content.as_deref(), &fingerprint);
                                        if record.before_capture_digest.as_deref()
                                            == Some(digest.as_str())
                                        {
                                            ServiceResponse::Action(record)
                                        } else {
                                            ServiceResponse::Error {
                                                code: "capture idempotency conflict".into(),
                                            }
                                        }
                                    }
                                    Ok(_) => match coordinator
                                        .capture_before(
                                            &envelope.request.action_id,
                                            content.map(Bytes::from),
                                            fingerprint,
                                        )
                                        .await
                                    {
                                        Ok(record) => ServiceResponse::Action(record),
                                        Err(error) => ServiceResponse::Error {
                                            code: error.to_string(),
                                        },
                                    },
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                }
                            }
                            Err(error) => ServiceResponse::Error {
                                code: format!("{error:?}"),
                            },
                        },
                        Ok(ServiceRequest::PrepareBatch { batch_version, envelope, entries } | ServiceRequest::PrepareBatchOnce { batch_version, envelope, entries }) if batch_version != 1 => {
                            if auth_bindings.authenticate(&envelope.auth).is_ok_and(|binding| binding.drain_session_id.is_none()) {
                                let staged_ids = owned_staged_ids_for_entries(
                                    &staged_uploads,
                                    &entries,
                                    &envelope.auth.actor_id,
                                    &envelope.auth.account_id,
                                    &envelope.request.action_id,
                                )
                                .await;
                                remove_owned_staged_uploads(
                                    &staged_uploads,
                                    &staged_bytes,
                                    &envelope.auth.actor_id,
                                    &envelope.auth.account_id,
                                    &envelope.request.action_id,
                                    &staged_ids,
                                )
                                .await;
                            }
                            ServiceResponse::Error {
                                code: format!(
                                    "unsupported mutation batch version: {batch_version}"
                                ),
                            }
                        }
                        Ok(request @ (ServiceRequest::PrepareBatch { .. } | ServiceRequest::PrepareBatchOnce { .. })) => {
                            let (envelope, entries, once) = match request {
                                ServiceRequest::PrepareBatch { envelope, entries, .. } => (envelope, entries, false),
                                ServiceRequest::PrepareBatchOnce { envelope, entries, .. } => (envelope, entries, true),
                                _ => unreachable!(),
                            };
                            let authenticated = auth_bindings.authenticate(&envelope.auth).is_ok_and(|binding| binding.drain_session_id.is_none());
                            let authorization = match auth_bindings
                                .validate_request_cap(&envelope, "capture")
                            {
                                Err(error) => {
                                    if authenticated {
                                        let staged_ids = owned_staged_ids_for_entries(
                                            &staged_uploads,
                                            &entries,
                                            &envelope.auth.actor_id,
                                            &envelope.auth.account_id,
                                            &envelope.request.action_id,
                                        )
                                        .await;
                                        remove_owned_staged_uploads(
                                            &staged_uploads,
                                            &staged_bytes,
                                            &envelope.auth.actor_id,
                                            &envelope.auth.account_id,
                                            &envelope.request.action_id,
                                            &staged_ids,
                                        )
                                        .await;
                                    }
                                    Err(error)
                                }
                                Ok(_)
                                    if request_resources_allowed(
                                        &envelope.request,
                                        &envelope.auth.actor_id,
                                        &envelope.auth.account_id,
                                        &grants,
                                    ) =>
                                {
                                    Ok(())
                                }
                                Ok(_) => {
                                    let staged_ids = owned_staged_ids_for_entries(
                                        &staged_uploads,
                                        &entries,
                                        &envelope.auth.actor_id,
                                        &envelope.auth.account_id,
                                        &envelope.request.action_id,
                                    )
                                    .await;
                                    remove_owned_staged_uploads(
                                        &staged_uploads,
                                        &staged_bytes,
                                        &envelope.auth.actor_id,
                                        &envelope.auth.account_id,
                                        &envelope.request.action_id,
                                        &staged_ids,
                                    )
                                    .await;
                                    Err(openclank_history::protocol::ProtocolError::AccountMismatch)
                                }
                            };
                            match authorization {
                                Ok(()) => {
                                    let staged_ids = owned_staged_ids_for_entries(
                                        &staged_uploads,
                                        &entries,
                                        &envelope.auth.actor_id,
                                        &envelope.auth.account_id,
                                        &envelope.request.action_id,
                                    )
                                    .await;
                                    let bound_error = if entries.is_empty()
                                        || entries.len() > MAX_BATCH_RESOURCES
                                    {
                                        Some(format!(
                                            "mutation batch resource count exceeds {}",
                                            MAX_BATCH_RESOURCES
                                        ))
                                    } else {
                                        None
                                    };
                                    let inline_bytes = entries
                                        .iter()
                                        .filter_map(|entry| entry.content.as_ref())
                                        .try_fold(0usize, |total, bytes| {
                                            total.checked_add(bytes.len())
                                        });
                                    let bound_error = bound_error.or_else(|| match inline_bytes {
                                        None => {
                                            Some("mutation batch inline byte count overflow".into())
                                        }
                                        Some(total) if total > MAX_BATCH_INLINE_BYTES => {
                                            Some(format!(
                                                "mutation batch inline bytes exceed {}",
                                                MAX_BATCH_INLINE_BYTES
                                            ))
                                        }
                                        Some(_) => None,
                                    });
                                    if let Some(error) = bound_error {
                                        remove_owned_staged_uploads(
                                            &staged_uploads,
                                            &staged_bytes,
                                            &envelope.auth.actor_id,
                                            &envelope.auth.account_id,
                                            &envelope.request.action_id,
                                            &staged_ids,
                                        )
                                        .await;
                                        ServiceResponse::Error { code: error }
                                    } else {
                                        let mut inputs = Vec::with_capacity(entries.len());
                                        let mut seen_stage_ids = BTreeSet::new();
                                        let mut staged_error = entries
                                            .iter()
                                            .filter_map(|entry| entry.staged_upload_id.as_ref())
                                            .find_map(|upload_id| {
                                                seen_stage_ids
                                                    .insert(upload_id.clone())
                                                    .then_some(())
                                                    .is_none()
                                                    .then(|| {
                                                        "duplicate staged upload reference in mutation batch"
                                                            .to_owned()
                                                    })
                                            });
                                        if staged_error.is_none() {
                                            for entry in entries {
                                                if entry.content.is_some()
                                                    && entry.staged_upload_id.is_some()
                                                {
                                                    staged_error = Some("batch entry must use inline content or staged content, not both".into());
                                                    break;
                                                }
                                                if let Some(expected_len) = entry.coverage.byte_len
                                                {
                                                    let actual_len =
                                                        entry.content.as_ref().map_or(0, Vec::len);
                                                    if entry.staged_upload_id.is_none()
                                                        && expected_len != actual_len as u64
                                                    {
                                                        staged_error = Some("batch coverage byte length does not match inline content".into());
                                                        break;
                                                    }
                                                }
                                                let content = if let Some(upload_id) =
                                                    entry.staged_upload_id.clone()
                                                {
                                                    let captured =
                                                        staged_ids.iter().find(|captured| {
                                                            captured.upload_id == upload_id
                                                        });
                                                    match captured {
                                                        Some(captured) => read_owned_staged_upload(
                                                            &staged_uploads,
                                                            captured,
                                                            &envelope.auth.actor_id,
                                                            &envelope.auth.account_id,
                                                            &envelope.request.action_id,
                                                            &entry.fingerprint,
                                                        )
                                                        .await
                                                        .map(Some)
                                                        .unwrap_or_else(|error| {
                                                            staged_error = Some(error);
                                                            None
                                                        }),
                                                        None => {
                                                            staged_error = Some(
                                                                "staged batch upload was replaced or removed".into(),
                                                            );
                                                            None
                                                        }
                                                    }
                                                } else {
                                                    entry.content.map(Bytes::from)
                                                };
                                                if let Some(expected_len) = entry.coverage.byte_len
                                                {
                                                    let actual_len = content
                                                        .as_ref()
                                                        .map_or(0, |bytes| bytes.len());
                                                    if expected_len != actual_len as u64 {
                                                        staged_error = Some("batch coverage byte length does not match staged content".into());
                                                    }
                                                }
                                                if staged_error.is_some() {
                                                    break;
                                                }
                                                inputs.push(BatchCaptureInput {
                                                    resource_key: entry.resource_key,
                                                    old_locator: entry.old_locator,
                                                    new_locator: entry.new_locator,
                                                    expected_revision: entry.expected_revision,
                                                    existence: entry.existence,
                                                    resource_type: entry.resource_type,
                                                    metadata: entry.metadata,
                                                    content,
                                                    fingerprint: entry.fingerprint,
                                                    coverage: entry.coverage,
                                                });
                                            }
                                        }
                                        if let Some(error) = staged_error {
                                            remove_owned_staged_uploads(
                                                &staged_uploads,
                                                &staged_bytes,
                                                &envelope.auth.actor_id,
                                                &envelope.auth.account_id,
                                                &envelope.request.action_id,
                                                &staged_ids,
                                            )
                                            .await;
                                            ServiceResponse::Error { code: error }
                                        } else {
                                            let once_marker = envelope.request.coverage.as_ref().and_then(|c| c.metadata.as_ref()).and_then(|m| m.get("batch_once"));
                                            let marker_valid = once_marker.and_then(|v| v.as_object()).is_some_and(|m| m.len() == 2 && m.get("version").and_then(|v| v.as_u64()) == Some(1) && m.get("binding_sha256").and_then(|v| v.as_str()).is_some_and(|v| v.len() == 64 && v.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))));
                                            if once && (!marker_valid || inputs.first().and_then(|e| e.metadata.opaque.as_ref()) != once_marker) {
                                                remove_owned_staged_uploads(&staged_uploads, &staged_bytes, &envelope.auth.actor_id, &envelope.auth.account_id, &envelope.request.action_id, &staged_ids).await;
                                                ServiceResponse::Error { code: "invalid batch once binding".into() }
                                            } else {
                                            let mut trusted_request = envelope.request.clone();
                                            trusted_request.physical_lease_keys =
                                                trusted_registry_aliases(&trusted_request, &resource_map);
                                            match begin(coordinator.catalog(), trusted_request) {
                                                Ok(BeginResult::Existing(record)) if once => {
                                                    remove_owned_staged_uploads(&staged_uploads, &staged_bytes, &envelope.auth.actor_id, &envelope.auth.account_id, &envelope.request.action_id, &staged_ids).await;
                                                    if record.before_capture_digest.as_ref().is_some_and(|d| d != &batch_capture_digest(&inputs)) {
                                                        ServiceResponse::Error { code: "history_prepare_conflict: existing batch digest differs".into() }
                                                    } else { ServiceResponse::BatchOnce { created: false, action: record } }
                                                }
                                                Ok(BeginResult::Existing(record))
                                                    if !matches!(
                                                        record.state,
                                                        ActionState::Intent
                                                            | ActionState::CaptureFailed
                                                    ) =>
                                                {
                                                    let incoming_digest =
                                                        batch_capture_digest(&inputs);
                                                    if record.before_capture_digest.as_deref()
                                                        == Some(incoming_digest.as_str())
                                                    {
                                                        remove_owned_staged_uploads(
                                                            &staged_uploads,
                                                            &staged_bytes,
                                                            &envelope.auth.actor_id,
                                                            &envelope.auth.account_id,
                                                            &envelope.request.action_id,
                                                            &staged_ids,
                                                        )
                                                        .await;
                                                        ServiceResponse::Action(record)
                                                    } else {
                                                        remove_owned_staged_uploads(
                                                            &staged_uploads,
                                                            &staged_bytes,
                                                            &envelope.auth.actor_id,
                                                            &envelope.auth.account_id,
                                                            &envelope.request.action_id,
                                                            &staged_ids,
                                                        )
                                                        .await;
                                                        ServiceResponse::Error {
                                                        code: "history_prepare_conflict: existing batch digest differs".into(),
                                                    }
                                                    }
                                                }
                                                Ok(_) => match coordinator
                                                    .capture_batch_before(
                                                        &envelope.request.action_id,
                                                        inputs,
                                                    )
                                                    .await
                                                {
                                                    Ok(record) => {
                                                        remove_owned_staged_uploads(
                                                            &staged_uploads,
                                                            &staged_bytes,
                                                            &envelope.auth.actor_id,
                                                            &envelope.auth.account_id,
                                                            &envelope.request.action_id,
                                                            &staged_ids,
                                                        )
                                                        .await;
                                                        if once { ServiceResponse::BatchOnce { created: true, action: record } } else { ServiceResponse::Action(record) }
                                                    }
                                                    Err(error) => {
                                                        remove_owned_staged_uploads(
                                                            &staged_uploads,
                                                            &staged_bytes,
                                                            &envelope.auth.actor_id,
                                                            &envelope.auth.account_id,
                                                            &envelope.request.action_id,
                                                            &staged_ids,
                                                        )
                                                        .await;
                                                        ServiceResponse::Error {
                                                            code: error.to_string(),
                                                        }
                                                    }
                                                },
                                                Err(error) => {
                                                    remove_owned_staged_uploads(
                                                        &staged_uploads,
                                                        &staged_bytes,
                                                        &envelope.auth.actor_id,
                                                        &envelope.auth.account_id,
                                                        &envelope.request.action_id,
                                                        &staged_ids,
                                                    )
                                                    .await;
                                                    ServiceResponse::Error {
                                                        code: error.to_string(),
                                                    }
                                                }
                                            }
                                            }
                                        }
                                    }
                                }
                                Err(error) => ServiceResponse::Error {
                                    code: format!("{error:?}"),
                                },
                            }
                        }
                        Ok(ServiceRequest::StageBegin {
                            envelope,
                            upload_id,
                            content_length,
                            fingerprint,
                        }) => match auth_bindings
                            .validate_stage(&envelope)
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) if !valid_upload_id(&upload_id) => ServiceResponse::Error {
                                code: "invalid staged upload id".into(),
                            },
                            Ok(_) if !staged_fingerprint_valid(&fingerprint, content_length) => {
                                ServiceResponse::Error {
                                    code:
                                        "staged upload fingerprint must be sha256:<digest>:<length>"
                                            .into(),
                                }
                            }
                            Ok(_) if content_length > staging_quota => ServiceResponse::Error {
                                code: "staged upload exceeds configured staging quota".into(),
                            },
                            Ok(_) => {
                                if let Err(error) = staging_budget_error(
                                    &coordinator,
                                    &staging_root,
                                    content_length,
                                    staged_bytes.load(Ordering::Acquire),
                                ) {
                                    ServiceResponse::Error { code: error }
                                } else {
                                    let mut uploads = staged_uploads.lock().await;
                                    if uploads.len() >= max_active_stages {
                                        ServiceResponse::Error {
                                            code: "staged upload concurrency quota is full".into(),
                                        }
                                    } else if uploads.contains_key(&upload_id) {
                                        ServiceResponse::Error {
                                            code: "staged upload id is already active".into(),
                                        }
                                    } else {
                                        let admitted = staged_bytes
                                            .fetch_update(
                                                Ordering::AcqRel,
                                                Ordering::Acquire,
                                                |used| {
                                                    used.checked_add(content_length)
                                                        .filter(|next| *next <= staging_quota)
                                                },
                                            )
                                            .is_ok();
                                        if !admitted {
                                            ServiceResponse::Error {
                                                code:
                                                    "staged upload exceeds configured staging quota"
                                                        .into(),
                                            }
                                        } else {
                                            let path =
                                                staged_upload_path(&staging_root, &upload_id);
                                            match std::fs::OpenOptions::new()
                                                .write(true)
                                                .create_new(true)
                                                .open(&path)
                                            {
                                                Ok(file) => {
                                                    #[cfg(unix)]
                                                    {
                                                                                                            let _ = file.set_permissions(
                                                            std::fs::Permissions::from_mode(0o600),
                                                        );
                                                    }
                                                    uploads.insert(
                                                        upload_id,
                                                        StagedUpload {
                                                            read_file:None, read_version_id:None, read_digest:None, worktree_completion_issued:false,
                                                            checkpoint_slot_not_started: false,
                                                stage_file_ready: true,
                                                            actor_id: envelope.auth.actor_id,
                                                            account_id: envelope.auth.account_id,
                                                            action_id: envelope.action_id,
                                                            content_length,
                                                            written: 0,
                                                            fingerprint,
                                                            finished: false,
                                                            last_activity_millis:
                                                                registry_timestamp(),
                                                            path,
                                                            io_lock: Arc::new(
                                                                tokio::sync::Mutex::new(()),
                                                            ),
                                                            io_active_count: Arc::new(
                                                                std::sync::atomic::AtomicUsize::new(
                                                                    0,
                                                                ),
                                                            ),
                                                        },
                                                    );
                                                    ServiceResponse::Accepted
                                                }
                                                Err(error) => {
                                                    staged_bytes.fetch_sub(
                                                        content_length,
                                                        Ordering::AcqRel,
                                                    );
                                                    ServiceResponse::Error {
                                                        code: format!(
                                                            "staged upload could not start: {error}"
                                                        ),
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::StageChunk {
                            envelope,
                            upload_id,
                            offset,
                            content,
                        }) => match auth_bindings
                            .validate_stage(&envelope)
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) => match content {
                                None => ServiceResponse::Error {
                                    code: "staged upload chunk is missing".into(),
                                },
                                Some(content) if content.len() > MAX_STAGE_CHUNK => {
                                    ServiceResponse::Error {
                                        code: "staged upload chunk is too large".into(),
                                    }
                                }
                                Some(content) => {
                                    let mut uploads = staged_uploads.lock().await;
                                    match uploads.get_mut(&upload_id) {
                                        None => ServiceResponse::Error {
                                            code: "staged upload is unknown".into(),
                                        },
                                        Some(upload)
                                            if upload.account_id != envelope.auth.account_id
                                                || upload.actor_id != envelope.auth.actor_id
                                                || upload.action_id != envelope.action_id =>
                                        {
                                            ServiceResponse::Error {
                                                code: "staged upload identity mismatch".into(),
                                            }
                                        }
                                        Some(upload)
                                            if upload.finished || offset != upload.written =>
                                        {
                                            ServiceResponse::Error {
                                                code: "staged upload offset is invalid".into(),
                                            }
                                        }
                                        Some(upload)
                                            if upload
                                                .written
                                                .saturating_add(content.len() as u64)
                                                > upload.content_length =>
                                        {
                                            ServiceResponse::Error {
                                                code: "staged upload exceeds declared length"
                                                    .into(),
                                            }
                                        }
                                        Some(upload) => {
                                            let result = (|| {
                                                let mut file = std::fs::OpenOptions::new()
                                                    .append(true)
                                                    .open(&upload.path)?;
                                                file.write_all(&content)?;
                                                file.sync_data()
                                            })(
                                            );
                                            match result {
                                                Ok(()) => {
                                                    upload.written += content.len() as u64;
                                                    upload.last_activity_millis =
                                                        registry_timestamp();
                                                    ServiceResponse::Accepted
                                                }
                                                Err(error) => ServiceResponse::Error {
                                                    code: format!(
                                                        "staged upload write failed: {error}"
                                                    ),
                                                },
                                            }
                                        }
                                    }
                                }
                            },
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::StageFinish {
                            envelope,
                            upload_id,
                        }) => match auth_bindings
                            .validate_stage(&envelope)
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) => {
                                let mut uploads = staged_uploads.lock().await;
                                match uploads.get_mut(&upload_id) {
                                    None => ServiceResponse::Error {
                                        code: "staged upload is unknown".into(),
                                    },
                                    Some(upload)
                                        if upload.account_id != envelope.auth.account_id
                                            || upload.actor_id != envelope.auth.actor_id
                                            || upload.action_id != envelope.action_id =>
                                    {
                                        ServiceResponse::Error {
                                            code: "staged upload identity mismatch".into(),
                                        }
                                    }
                                    Some(upload) if upload.written != upload.content_length => {
                                        ServiceResponse::Error {
                                            code: "staged upload is incomplete".into(),
                                        }
                                    }
                                    Some(upload) => match verify_staged_upload(upload) {
                                        Ok(()) => {
                                            upload.finished = true;
                                            upload.last_activity_millis = registry_timestamp();
                                            ServiceResponse::Staged {
                                                upload_id,
                                                content_length: upload.content_length,
                                            }
                                        }
                                        Err(error) => ServiceResponse::Error { code: error },
                                    },
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::StageAbort {
                            envelope,
                            upload_id,
                        }) => match auth_bindings
                            .validate_stage(&envelope)
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) => {
                                let mut uploads = staged_uploads.lock().await;
                                match uploads.get(&upload_id) {
                                    Some(upload)
                                        if upload.account_id == envelope.auth.account_id
                                            && upload.actor_id == envelope.auth.actor_id
                                            && upload.action_id == envelope.action_id =>
                                    {
                                        let upload =
                                            uploads.remove(&upload_id).expect("upload exists");
                                        remove_staged_upload(&upload, &staged_bytes);
                                        ServiceResponse::Accepted
                                    }
                                    Some(_) => ServiceResponse::Error {
                                        code: "staged upload identity mismatch".into(),
                                    },
                                    None => ServiceResponse::Accepted,
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::PrepareStaged {
                            envelope,
                            upload_id,
                            fingerprint,
                        }) => match auth_bindings
                            .validate_request_cap(&envelope, "capture")
                            .and_then(|_| {
                                if request_resources_allowed(
                                    &envelope.request,
                                    &envelope.auth.actor_id,
                                    &envelope.auth.account_id,
                                    &grants,
                                ) {
                                    Ok(())
                                } else {
                                    Err(ProtocolError::AccountMismatch)
                                }
                            }) {
                            Ok(()) => {
                                let staged = {
                                    let uploads = staged_uploads.lock().await;
                                    uploads.get(&upload_id).cloned()
                                };
                                match staged {
                                    None => ServiceResponse::Error {
                                        code: "staged upload is unknown".into(),
                                    },
                                    Some(staged)
                                        if staged.account_id != envelope.auth.account_id
                                            || staged.actor_id != envelope.auth.actor_id
                                            || staged.action_id != envelope.request.action_id
                                            || !staged.finished
                                            || staged.fingerprint != fingerprint =>
                                    {
                                        ServiceResponse::Error {
                                            code: "staged upload identity or fingerprint mismatch"
                                                .into(),
                                        }
                                    }
                                    Some(staged) => {
                                        let result = match verify_staged_upload(&staged) {
                                            Err(error) => Err(error),
                                            Ok(()) => match std::fs::read(&staged.path) {
                                                Ok(bytes) => match begin(
                                                    coordinator.catalog(),
                                                    envelope.request.clone(),
                                                ) {
                                                    Ok(BeginResult::Existing(record))
                                                        if !matches!(
                                                            record.state,
                                                            ActionState::Intent
                                                                | ActionState::CaptureFailed
                                                        ) =>
                                                    {
                                                        let digest = capture_input_digest(
                                                            Some(&bytes),
                                                            &fingerprint,
                                                        );
                                                        if record.before_capture_digest.as_deref()
                                                            == Some(digest.as_str())
                                                        {
                                                            Ok(record)
                                                        } else {
                                                            Err("capture idempotency conflict"
                                                                .into())
                                                        }
                                                    }
                                                    Ok(_) => coordinator
                                                        .capture_before(
                                                            &envelope.request.action_id,
                                                            Some(Bytes::from(bytes)),
                                                            staged.fingerprint.clone(),
                                                        )
                                                        .await
                                                        .map_err(|error| error.to_string()),
                                                    Err(error) => Err(error.to_string()),
                                                },
                                                Err(error) => Err(error.to_string()),
                                            },
                                        };
                                        let removed = {
                                            let mut uploads = staged_uploads.lock().await;
                                            uploads.remove(&upload_id)
                                        };
                                        if let Some(upload) = removed.as_ref() {
                                            remove_staged_upload(upload, &staged_bytes);
                                        }
                                        match result {
                                            Ok(record) => ServiceResponse::Action(record),
                                            Err(error) => ServiceResponse::Error {
                                                code: format!(
                                                    "staged before capture failed: {error}"
                                                ),
                                            },
                                        }
                                    }
                                }
                            }
                            Err(error) => ServiceResponse::Error {
                                code: format!("{error:?}"),
                            },
                        },
                        Ok(ServiceRequest::CompleteStaged {
                            envelope,
                            upload_id,
                            fingerprint,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                            .and_then(|_| {
                                authorize(
                                    &coordinator,
                                    &envelope,
                                    &envelope.auth.account_id,
                                    &grants,
                                )
                                .map(|_| ())
                                .map_err(|error| error.to_string())
                            }) {
                            Ok(()) => {
                                let staged = {
                                    let uploads = staged_uploads.lock().await;
                                    uploads.get(&upload_id).cloned()
                                };
                                match staged {
                                    None => ServiceResponse::Error {
                                        code: "staged upload is unknown".into(),
                                    },
                                    Some(staged)
                                        if staged.account_id != envelope.auth.account_id
                                            || staged.actor_id != envelope.auth.actor_id
                                            || staged.action_id != envelope.action_id
                                            || !staged.finished
                                            || staged.fingerprint != fingerprint =>
                                    {
                                        ServiceResponse::Error {
                                            code: "staged upload identity or fingerprint mismatch"
                                                .into(),
                                        }
                                    }
                                    Some(staged) => {
                                        let result = match verify_staged_upload(&staged) {
                                            Err(error) => Err(error),
                                            Ok(()) => match std::fs::read(&staged.path) {
                                                Ok(bytes) => coordinator
                                                    .capture_after(
                                                        &envelope.action_id,
                                                        Some(Bytes::from(bytes)),
                                                        staged.fingerprint.clone(),
                                                    )
                                                    .await
                                                    .map_err(|error| error.to_string()),
                                                Err(error) => Err(error.to_string()),
                                            },
                                        };
                                        let removed = {
                                            let mut uploads = staged_uploads.lock().await;
                                            uploads.remove(&upload_id)
                                        };
                                        if let Some(upload) = removed.as_ref() {
                                            remove_staged_upload(upload, &staged_bytes);
                                        }
                                        match result {
                                            Ok(record) => ServiceResponse::Action(record),
                                            Err(error) => ServiceResponse::Error {
                                                code: format!(
                                                    "staged after capture failed: {error}"
                                                ),
                                            },
                                        }
                                    }
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::RebindResource {
                            envelope,
                            resource_id,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                            .and_then(|_| {
                                if resource_id.trim().is_empty()
                                    || resource_id.len() > 256
                                    || resource_id.chars().any(|character| {
                                        character.is_whitespace()
                                            || matches!(character, '/' | '\\' | '\0')
                                    })
                                {
                                    return Err("resource rebind id is not opaque".to_owned());
                                }
                                authorize(
                                    &coordinator,
                                    &envelope,
                                    &envelope.auth.account_id,
                                    &grants,
                                )
                                .map_err(|error| error.to_string())
                            }) {
                            Ok(action) => {
                                let mut resource_key = action.resource_key.clone();
                                resource_key.resource_id = resource_id;
                                match coordinator.rebind_resource(&envelope.action_id, resource_key)
                                {
                                    Ok(record) => ServiceResponse::Action(record),
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::RecordLive { envelope, receipt }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                            .and_then(|_| {
                                authorize(
                                    &coordinator,
                                    &envelope,
                                    &envelope.auth.account_id,
                                    &grants,
                                )
                            }) {
                            Ok(record) if matches!(record.state, ActionState::Applied) => {
                                if record.live.as_ref() == Some(&receipt) {
                                    ServiceResponse::Action(record)
                                } else {
                                    ServiceResponse::Error {
                                        code: "live receipt conflict".into(),
                                    }
                                }
                            }
                            Ok(record) => {
                                match (if record.state == ActionState::BeforeDurable {
                                    coordinator.begin_apply(&envelope.action_id)
                                } else {
                                    Ok(record)
                                })
                                .and_then(|_record| {
                                    #[cfg(feature = "test_faults")]
                                    qualification_abort("applying");
                                    coordinator.record_live(&envelope.action_id, receipt)
                                }) {
                                    Ok(record) => ServiceResponse::Action(record),
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::Complete {
                            envelope,
                            content,
                            fingerprint,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                            .and_then(|_| {
                                authorize(
                                    &coordinator,
                                    &envelope,
                                    &envelope.auth.account_id,
                                    &grants,
                                )
                            }) {
                            Ok(record) if record.state == ActionState::Complete => {
                                let digest = capture_input_digest(content.as_deref(), &fingerprint);
                                if record.after_capture_digest.as_deref() == Some(digest.as_str()) {
                                    ServiceResponse::Action(record)
                                } else {
                                    ServiceResponse::Error {
                                        code: "after capture idempotency conflict".into(),
                                    }
                                }
                            }
                            Ok(_) => match coordinator
                                .capture_after(
                                    &envelope.action_id,
                                    content.map(Bytes::from),
                                    fingerprint,
                                )
                                .await
                                .and_then(|_| {
                                    #[cfg(feature = "test_faults")]
                                    qualification_abort("after_durable");
                                    coordinator.complete(&envelope.action_id)
                                })
                                .map(|record| {
                                    #[cfg(feature = "test_faults")]
                                    qualification_abort("complete");
                                    record
                                }) {
                                Ok(record) => ServiceResponse::Action(record),
                                Err(error) => ServiceResponse::Error {
                                    code: error.to_string(),
                                },
                            },
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::CompleteBatch {
                            envelope,
                            batch_version,
                            entries,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                            .and_then(|_| {
                                authorize(
                                    &coordinator,
                                    &envelope,
                                    &envelope.auth.account_id,
                                    &grants,
                                )
                            }) {
                            Ok(_record) if batch_version != 1 => ServiceResponse::Error {
                                code: format!("unsupported mutation batch version: {batch_version}"),
                            },
                            Ok(record) if record.state == ActionState::Complete => {
                                let digest = complete_batch_digest(&entries);
                                if record.complete_request_digest.as_deref() == Some(digest.as_str()) {
                                    ServiceResponse::Action(record)
                                } else {
                                    ServiceResponse::Error { code: "complete batch retry payload mismatch".into() }
                                }
                            }
                            Ok(_) if entries.is_empty() || entries.len() > MAX_BATCH_RESOURCES => {
                                ServiceResponse::Error {
                                    code: "after-state batch resource count is invalid".into(),
                                }
                            }
                            Ok(_) => {
                                let request_digest = complete_batch_digest(&entries);
                                if let Err(error) = coordinator.catalog().bind_complete_request(&envelope.action_id, &request_digest) {
                                    ServiceResponse::Error { code: error.to_string() }
                                } else {
                                    let mut inputs = Vec::with_capacity(entries.len());
                                    let mut error = None;
                                    for entry in entries {
                                    if entry.content.is_some() && entry.staged_upload_id.is_some() {
                                        error = Some("batch after entry must use inline content or staged content, not both".to_owned());
                                        break;
                                    }
                                    let content = if let Some(upload_id) = entry.staged_upload_id.as_ref() {
                                        let staged = {
                                            let uploads = staged_uploads.lock().await;
                                            uploads.get(upload_id).cloned()
                                        };
                                        match staged {
                                            Some(staged)
                                                if staged.account_id == envelope.auth.account_id
                                                    && staged.actor_id == envelope.auth.actor_id
                                                    && staged.action_id == envelope.action_id
                                                    && staged.finished
                                                    && staged.fingerprint == entry.fingerprint => {
                                                match verify_staged_upload(&staged)
                                                    .and_then(|_| std::fs::read(&staged.path).map_err(|e| e.to_string()))
                                                {
                                                    Ok(bytes) => {
                                                        let mut uploads = staged_uploads.lock().await;
                                                        if let Some(removed) = uploads.remove(upload_id) {
                                                            remove_staged_upload(&removed, &staged_bytes);
                                                        }
                                                        Some(Bytes::from(bytes))
                                                    }
                                                    Err(reason) => {
                                                        error = Some(reason);
                                                        None
                                                    }
                                                }
                                            }
                                            Some(_) => {
                                                error = Some("staged after upload identity or fingerprint mismatch".into());
                                                None
                                            }
                                            None => {
                                                error = Some("staged after upload is unknown".into());
                                                None
                                            }
                                        }
                                    } else {
                                        entry.content.map(Bytes::from)
                                    };
                                    if error.is_some() {
                                        break;
                                    }
                                    if let Some(expected) = entry.coverage.byte_len {
                                        if expected != content.as_ref().map_or(0, |bytes| bytes.len()) as u64 {
                                            error = Some("after-state coverage byte length does not match content".into());
                                            break;
                                        }
                                    }
                                    inputs.push(BatchAfterInput {
                                        resource_key: entry.resource_key,
                                        locator: entry.locator,
                                        existence: entry.existence,
                                        resource_type: entry.resource_type,
                                        metadata: entry.metadata,
                                        content,
                                        fingerprint: entry.fingerprint,
                                        coverage: entry.coverage,
                                        outcome: entry.outcome,
                                    });
                                    }
                                    if let Some(reason) = error {
                                        ServiceResponse::Error { code: reason }
                                    } else {
                                        let result = coordinator
                                            .capture_after_batch(&envelope.action_id, inputs)
                                            .await
                                            .and_then(|_| coordinator.complete(&envelope.action_id));
                                        match result {
                                            Ok(record) => ServiceResponse::Action(record),
                                            Err(error) => ServiceResponse::Error {
                                                code: error.to_string(),
                                            },
                                        }
                                    }
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::Abort { envelope }) => {
                            match auth_bindings
                                .validate_control_cap(&envelope, "capture")
                                .map_err(|error| format!("{error:?}"))
                                .and_then(|_| {
                                    authorize(
                                        &coordinator,
                                        &envelope,
                                        &envelope.auth.account_id,
                                        &grants,
                                    )
                                }) {
                                Ok(_) => match coordinator.abort(&envelope.action_id) {
                                    Ok(record) => ServiceResponse::Action(record),
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                },
                                Err(error) => ServiceResponse::Error { code: error },
                            }
                        }
                        Ok(ServiceRequest::RegisterResource {
                            envelope,
                            account_id,
                            workspace_id,
                            root_id,
                            root_path,
                            relative_path,
                            resource_id,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) if account_id != envelope.auth.account_id => {
                                ServiceResponse::Error {
                                    code: "resource account binding mismatch".into(),
                                }
                            }
                            Ok(_) => {
                                match ResourceRegistry::load(&resource_map).and_then(|registry| {
                                    let suggested = resource_id.as_deref().unwrap_or_default();
                                    let (registration, created) = registry_registration(
                                        &registry,
                                        &envelope.auth.actor_id,
                                        suggested,
                                        &account_id,
                                        &workspace_id,
                                        root_id.as_deref(),
                                        &root_path,
                                        &relative_path,
                                        &host_root,
                                        &authorized_roots,
                                        false,
                                        None,
                                    )
                                    .map_err(std::io::Error::other)?;
                                    let mut next = registry;
                                    next.entries.insert(
                                        registration.resource_id.clone(),
                                        registration.clone(),
                                    );
                                    next.persist(&resource_map)?;
                                    Ok((registration, created))
                                }) {
                                    Ok((registration, created)) => ServiceResponse::Resource {
                                        handle: resource_handle(&registration),
                                        created,
                                    },
                                    Err(error) => ServiceResponse::Error {
                                        code: format!("resource registration failed: {error}"),
                                    },
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::RegisterManagedMediaDestination {
                            envelope,
                            account_id,
                            workspace_id,
                            root_id,
                            root_path,
                            relative_path,
                            resource_id,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) if account_id != envelope.auth.account_id => {
                                ServiceResponse::Error {
                                    code: "resource account binding mismatch".into(),
                                }
                            }
                            Ok(_) => {
                                match ResourceRegistry::load(&resource_map).and_then(|registry| {
                                    let suggested = resource_id.as_deref().unwrap_or_default();
                                    let (registration, created) = registry_registration(
                                        &registry,
                                        &envelope.auth.actor_id,
                                        suggested,
                                        &account_id,
                                        &workspace_id,
                                        root_id.as_deref(),
                                        &root_path,
                                        &relative_path,
                                        &host_root,
                                        &authorized_roots,
                                        true,
                                        None,
                                    )
                                    .map_err(std::io::Error::other)?;
                                    let mut next = registry;
                                    next.entries.insert(
                                        registration.resource_id.clone(),
                                        registration.clone(),
                                    );
                                    next.persist(&resource_map)?;
                                    Ok((registration, created))
                                }) {
                                    Ok((registration, created)) => ServiceResponse::Resource {
                                        handle: resource_handle(&registration),
                                        created,
                                    },
                                    Err(error) => ServiceResponse::Error {
                                        code: format!("resource registration failed: {error}"),
                                    },
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::RegisterCheckoutAbsent {envelope,account_id,workspace_id,root_id,root_path,relative_path,operation_id,paths,directories}) => match auth_bindings.validate_control_cap(&envelope,"capture").map_err(|error|format!("{error:?}")) {
                            Ok(_) if account_id==envelope.auth.account_id && operation_id.len()>=16 && operation_id.len()<=80 && operation_id.bytes().all(|c|c.is_ascii_alphanumeric()||c==b'-'||c==b'_') => {
                                let key=format!("{}\0{}\0{}",account_id,envelope.auth.actor_id,operation_id);let expected=format!("odysseus-managed-{:x}",Sha256::digest(key.as_bytes()));
                                if envelope.action_id!=expected {ServiceResponse::Error{code:"checkout registration unavailable".into()}} else {
                                    match ResourceRegistry::load(&resource_map).and_then(|mut registry| {
                                        let root=registry_checkout_absent(&envelope.auth.actor_id,&account_id,&workspace_id,&root_id,&root_path,&relative_path,&paths,&directories,&host_root,&authorized_roots).map_err(std::io::Error::other)?;
                                        let (registration,created)=registry_registration(&registry,&envelope.auth.actor_id,"",&account_id,&workspace_id,Some(&root_id),&root_path,&relative_path,&host_root,&authorized_roots,false,Some(root)).map_err(std::io::Error::other)?;
                                        if created {registry.entries.insert(registration.resource_id.clone(),registration.clone());registry.persist(&resource_map)?;}Ok((registration,created))
                                    }) {Ok((registration,created))=>ServiceResponse::Resource{handle:resource_handle(&registration),created},Err(_)=>ServiceResponse::Error{code:"checkout registration unavailable".into()}}
                                }
                            },
                            Ok(_)=>ServiceResponse::Error{code:"checkout registration unavailable".into()},Err(error)=>ServiceResponse::Error{code:error},
                        },
                        Ok(ServiceRequest::RegisterManagedFolderDestination {
                            envelope, account_id, workspace_id, root_id, root_path, operation_id,
                            source_resource_id, source_folder, destination_folder, asset_proof, destination_root_id, destination_root_path,
                        }) => match auth_bindings.validate_control_cap(&envelope, "capture").map_err(|error| format!("{error:?}")) {
                            Ok(_) if !MANAGED_REGISTRY_BATCH_READY || account_id != envelope.auth.account_id =>
                                ServiceResponse::Error { code:"managed folder registration unavailable".into() },
                            Ok(_) => {
                                let action_key = format!("{}\0{}\0{}", account_id, envelope.auth.actor_id, operation_id);
                                let expected_action = format!("odysseus-managed-{:x}", Sha256::digest(action_key.as_bytes()));
                                if envelope.action_id != expected_action {
                                    ServiceResponse::Error { code:"managed folder registration unavailable".into() }
                                } else {
                                    match ResourceRegistry::load(&resource_map).and_then(|mut registry| {
                                        let (registration, created) = registry_folder_destination(&registry, &envelope.auth.actor_id,
                                            &account_id, &workspace_id, &root_id, &root_path, &operation_id, &source_resource_id,
                                            &source_folder, &destination_folder, asset_proof.as_ref(), destination_root_id.as_deref(), destination_root_path.as_deref(), &host_root, &authorized_roots).map_err(std::io::Error::other)?;
                                        if created { registry.entries.insert(registration.resource_id.clone(), registration.clone()); registry.persist(&resource_map)?; }
                                        Ok((registration, created))
                                    }) {
                                        Ok((registration, created)) => ServiceResponse::Resource { handle:resource_handle(&registration), created },
                                        Err(_) => ServiceResponse::Error { code:"managed folder registration unavailable".into() },
                                    }
                                }
                            }
                            Err(error) => ServiceResponse::Error { code:error },
                        },
                        Ok(ServiceRequest::RegisteredSubtreeSnapshot {
                            envelope, account_id, workspace_id, root_id, root_path, source_relative_path, destination_relative_path, destination_root_id, destination_root_path,
                        }) => match auth_bindings.validate_control_cap(&envelope, "capture").map_err(|error| format!("{error:?}")) {
                            Ok(_) if !MANAGED_REGISTRY_BATCH_READY || account_id != envelope.auth.account_id =>
                                ServiceResponse::Error { code:"managed subtree is unavailable".into() },
                            Ok(_) => match ResourceRegistry::load(&resource_map).map_err(|_| "managed subtree is unavailable".to_owned())
                                .and_then(|registry| registry_subtree_snapshot(&registry, &envelope.auth.actor_id, &account_id, &workspace_id,
                                    &root_id, &root_path, &source_relative_path, &destination_relative_path, destination_root_id.as_deref(), destination_root_path.as_deref(), &host_root, &authorized_roots)) {
                                Ok(items) => ServiceResponse::RegisteredSubtreeSnapshot { items },
                                Err(_) => ServiceResponse::Error { code:"managed subtree is unavailable".into() },
                            },
                            Err(error) => ServiceResponse::Error { code:error },
                        },
                        Ok(ServiceRequest::ListRegisteredResources {
                            envelope,
                            cursor,
                            limit,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code: error },
                            Ok(_) if !(1..=100).contains(&limit) => ServiceResponse::Unavailable {
                                code: "invalid_limit".into(),
                                message: "registered resource page size must be between 1 and 100".into(),
                            },
                            Ok(_) => {
                                let start = match cursor.as_deref() {
                                    None => Some(0usize),
                                    Some(value) if value.len() <= 20 => value.parse::<usize>().ok(),
                                    Some(_) => None,
                                };
                                match (start, ResourceRegistry::load(&resource_map)) {
                                    (None, _) => ServiceResponse::Unavailable {
                                        code: "invalid_cursor".into(),
                                        message: "registered resource cursor is invalid".into(),
                                    },
                                    (_, Err(_)) => ServiceResponse::Unavailable {
                                        code: "resource_unavailable".into(),
                                        message: "registered Files resources are unavailable".into(),
                                    },
                                    (Some(start), Ok(registry)) => {
                                        let mut eligible = registry.entries.values().filter(|entry| {
                                            entry.account_id == envelope.auth.account_id
                                                && !entry.workspace_id.is_empty()
                                                && resolve_registered_path(
                                                    &envelope.auth.actor_id,
                                                entry,
                                                    &host_root,
                                                    &authorized_roots,
                                                )
                                                .is_ok()
                                        }).cloned().skip(start);
                                        let mut items = eligible.by_ref().take(usize::from(limit)).collect::<Vec<_>>();
                                        let has_more = eligible.next().is_some();
                                        let next_cursor = has_more
                                            .then(|| start.saturating_add(items.len()).to_string());
                                        ServiceResponse::RegisteredResources(
                                            openclank_history::protocol::RegisteredResourcePage {
                                                items: std::mem::take(&mut items),
                                                next_cursor,
                                            },
                                        )
                                    }
                                }
                            }
                        },
                        Ok(ServiceRequest::UpdateResource {
                            envelope,
                            resource_id,
                            account_id,
                            workspace_id,
                            root_id,
                            root_path,
                            relative_path,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) if account_id != envelope.auth.account_id => {
                                ServiceResponse::Error {
                                    code: "resource account binding mismatch".into(),
                                }
                            }
                            Ok(_) => match ResourceRegistry::load(&resource_map).and_then(
                                |mut registry| {
                                    let current =
                                        registry.entries.get(&resource_id).cloned().ok_or_else(
                                            || {
                                                std::io::Error::new(
                                                    std::io::ErrorKind::NotFound,
                                                    "resource id is unknown",
                                                )
                                            },
                                        )?;
                                    if !current.active
                                        || resolve_registered_path(&envelope.auth.actor_id, &current, &host_root, &authorized_roots).is_err()
                                        || current.account_id != account_id
                                        || current.workspace_id != workspace_id
                                    {
                                        return Err(std::io::Error::new(
                                            std::io::ErrorKind::PermissionDenied,
                                            "resource update is unauthorized",
                                        ));
                                    }
                                    let (destination_root_id, destination_root) =
                                        registry_destination(
                                            &envelope.auth.actor_id,
                                            &account_id,
                                            &workspace_id,
                                            root_id.as_deref(),
                                            &root_path,
                                            &relative_path,
                                            &host_root,
                                            &authorized_roots,
                                        )
                                        .map_err(std::io::Error::other)?;
                                    let replacement = (destination_root_id, destination_root);
                                    if registry
                                        .active_for_path(
                                            &account_id,
                                            &workspace_id,
                                            &replacement.1,
                                            &relative_path,
                                        )
                                        .is_some_and(|entry| entry.resource_id != resource_id)
                                    {
                                        return Err(std::io::Error::new(
                                            std::io::ErrorKind::AlreadyExists,
                                            "resource destination is already bound",
                                        ));
                                    }
                                    let mut updated = current;
                                    updated.actor_ids = authorized_roots.iter().find(|root| root.root_id == replacement.0).map(|root| root.actor_ids.clone()).unwrap_or_default();
                                    updated.root_id = replacement.0;
                                    updated.root_path = replacement.1;
                                    updated.relative_path = relative_path;
                                    updated.generation = updated.generation.saturating_add(1);
                                    updated.updated_millis = registry_timestamp();
                                    registry.entries.insert(resource_id, updated.clone());
                                    registry.persist(&resource_map)?;
                                    Ok(updated)
                                },
                            ) {
                                Ok(updated) => ServiceResponse::Resource {
                                    handle: resource_handle(&updated),
                                    created: false,
                                },
                                Err(error) => ServiceResponse::Error {
                                    code: format!("resource update failed: {error}"),
                                },
                            },
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::MoveResource {
                            envelope,
                            resource_id,
                            replaced_resource_id,
                            account_id,
                            workspace_id,
                            root_id,
                            root_path,
                            relative_path,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) if account_id != envelope.auth.account_id => {
                                ServiceResponse::Error {
                                    code: "resource account binding mismatch".into(),
                                }
                            }
                            Ok(_) => match ResourceRegistry::load(&resource_map).and_then(
                                |mut registry| {
                                    let mut current =
                                        registry.entries.get(&resource_id).cloned().ok_or_else(
                                            || {
                                                std::io::Error::new(
                                                    std::io::ErrorKind::NotFound,
                                                    "resource id is unknown",
                                                )
                                            },
                                        )?;
                                    if !current.active
                                        || resolve_registered_path(&envelope.auth.actor_id, &current, &host_root, &authorized_roots).is_err()
                                        || current.account_id != account_id
                                        || current.workspace_id != workspace_id
                                    {
                                        return Err(std::io::Error::new(
                                            std::io::ErrorKind::PermissionDenied,
                                            "resource move is unauthorized",
                                        ));
                                    }
                                    let (destination_root_id, destination_root) =
                                        registry_destination(
                                            &envelope.auth.actor_id,
                                            &account_id,
                                            &workspace_id,
                                            root_id.as_deref(),
                                            &root_path,
                                            &relative_path,
                                            &host_root,
                                            &authorized_roots,
                                        )
                                        .map_err(std::io::Error::other)?;
                                    let destination = registry
                                        .active_for_path(
                                            &account_id,
                                            &workspace_id,
                                            &destination_root,
                                            &relative_path,
                                        )
                                        .map(|entry| entry.resource_id.clone());
                                    if let Some(destination_id) = destination {
                                        if destination_id != resource_id
                                            && replaced_resource_id.as_deref()
                                                != Some(destination_id.as_str())
                                        {
                                            return Err(std::io::Error::new(
                                                std::io::ErrorKind::AlreadyExists,
                                                "resource destination is already bound",
                                            ));
                                        }
                                    }
                                    if let Some(replaced_id) = replaced_resource_id.as_deref() {
                                        if replaced_id != resource_id {
                                            let replaced = registry
                                                .entries
                                                .get_mut(replaced_id)
                                                .ok_or_else(|| {
                                                    std::io::Error::new(
                                                        std::io::ErrorKind::NotFound,
                                                        "replacement resource id is unknown",
                                                    )
                                                })?;
                                            if !replaced.active
                                                || replaced.account_id != account_id
                                                || replaced.workspace_id != workspace_id
                                                || replaced.root_path != destination_root
                                                || replaced.relative_path != relative_path
                                            {
                                                return Err(std::io::Error::new(
                                                    std::io::ErrorKind::PermissionDenied,
                                                    "replacement resource binding mismatch",
                                                ));
                                            }
                                            replaced.active = false;
                                            replaced.generation =
                                                replaced.generation.saturating_add(1);
                                            replaced.updated_millis = registry_timestamp();
                                        }
                                    }
                                    current.actor_ids = authorized_roots.iter().find(|root| root.root_id == destination_root_id).map(|root| root.actor_ids.clone()).unwrap_or_default();
                                    current.root_id = destination_root_id;
                                    current.root_path = destination_root;
                                    current.relative_path = relative_path;
                                    current.generation = current.generation.saturating_add(1);
                                    current.updated_millis = registry_timestamp();
                                    registry.entries.insert(resource_id, current.clone());
                                    // Registry updates are protected by the coordinator mutex for
                                    // the full request. Persisting once makes the move a single
                                    // durable transition: readers observe either old or new state.
                                    registry.persist(&resource_map)?;
                                    Ok(current)
                                },
                            ) {
                                Ok(updated) => ServiceResponse::Resource {
                                    handle: resource_handle(&updated),
                                    created: false,
                                },
                                Err(error) => ServiceResponse::Error {
                                    code: format!("resource move failed: {error}"),
                                },
                            },
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::MoveResourcesBatch { envelope, account_id, workspace_id, moves, subtree_guard, validate_only }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                            .and_then(|_| authorize(&coordinator, &envelope, &envelope.auth.account_id, &grants)) {
                            Ok(_) if !MANAGED_REGISTRY_BATCH_READY => ServiceResponse::Error { code:"managed registry batch unavailable".into() },
                            Ok(action) if account_id == envelope.auth.account_id => {
                                match ResourceRegistry::load(&resource_map).map_err(|_| "registry unavailable".to_owned()).and_then(|mut registry| {
                                    let changed = apply_registry_batch(&mut registry, &action, &envelope.auth.actor_id, &account_id, &workspace_id, &moves, subtree_guard.as_ref(), validate_only, &host_root, &authorized_roots)?;
                                    if changed { registry.persist(&resource_map).map_err(|_| "registry persist failed".to_owned())?; }
                                    Ok(())
                                }) {
                                    Ok(()) => ServiceResponse::Accepted,
                                    Err(error) => ServiceResponse::Error { code:error },
                                }
                            }
                            Ok(_) => ServiceResponse::Error { code:"resource account binding mismatch".into() },
                            Err(error) => ServiceResponse::Error { code:error },
                        },
                        Ok(ServiceRequest::RevokeResource {
                            envelope,
                            resource_id,
                            reason: _reason,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) => match ResourceRegistry::load(&resource_map).and_then(
                                |mut registry| {
                                    let mut entry =
                                        registry.entries.get(&resource_id).cloned().ok_or_else(
                                            || {
                                                std::io::Error::new(
                                                    std::io::ErrorKind::NotFound,
                                                    "resource id is unknown",
                                                )
                                            },
                                        )?;
                                    if entry.account_id != envelope.auth.account_id
                                        || resolve_registered_path(&envelope.auth.actor_id, &entry, &host_root, &authorized_roots).is_err() {
                                        return Err(std::io::Error::new(
                                            std::io::ErrorKind::PermissionDenied,
                                            "resource revoke is unauthorized",
                                        ));
                                    }
                                    if entry.active {
                                        entry.active = false;
                                        entry.generation = entry.generation.saturating_add(1);
                                        entry.updated_millis = registry_timestamp();
                                        registry.entries.insert(resource_id, entry);
                                        registry.persist(&resource_map)?;
                                    }
                                    Ok(())
                                },
                            ) {
                                Ok(()) => ServiceResponse::Accepted,
                                Err(error) => ServiceResponse::Error {
                                    code: format!("resource revoke failed: {error}"),
                                },
                            },
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::ResolveResource {
                            envelope,
                            resource_id,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Ok(_) => {
                                match ResourceRegistry::load(&resource_map).and_then(|registry| {
                                    let entry =
                                        registry.entries.get(&resource_id).cloned().ok_or_else(
                                            || {
                                                std::io::Error::new(
                                                    std::io::ErrorKind::NotFound,
                                                    "resource id is unknown",
                                                )
                                            },
                                        )?;
                                    if !entry.active
                                        || resolve_registered_path(&envelope.auth.actor_id, &entry, &host_root, &authorized_roots).is_err()
                                        || !owner_allowed(
                                            &entry.account_id,
                                            &envelope.auth.actor_id,
                                            &envelope.auth.account_id,
                                            &grants,
                                        )
                                    {
                                        return Err(std::io::Error::new(
                                            std::io::ErrorKind::PermissionDenied,
                                            "resource resolution is unauthorized",
                                        ));
                                    }
                                    Ok(entry)
                                }) {
                                    Ok(entry) => ServiceResponse::Resource {
                                        handle: resource_handle(&entry),
                                        created: false,
                                    },
                                    Err(error) => ServiceResponse::Error {
                                        code: format!("resource resolution failed: {error}"),
                                    },
                                }
                            }
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::ReadVersion { envelope, receipt }) => {
                            match auth_bindings
                                .validate_control_cap(&envelope, "read")
                                .map_err(|error| format!("{error:?}"))
                                .and_then(|_| {
                                    authorize(
                                        &coordinator,
                                        &envelope,
                                        &envelope.auth.account_id,
                                        &grants,
                                    )
                                }) {
                                Ok(_) => match coordinator
                                    .read_version(&envelope.action_id, &receipt)
                                    .await
                                {
                                    Ok(Some(bytes)) if bytes.len() > MAX_STAGE_CHUNK => {
                                        ServiceResponse::Error {
                                            code:
                                                "version payload requires bounded chunked readback"
                                                    .into(),
                                        }
                                    }
                                    Ok(bytes) => {
                                        ServiceResponse::Bytes(bytes.map(|value| value.to_vec()))
                                    }
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                },
                                Err(error) => ServiceResponse::Error { code: error },
                            }
                        }
                        Ok(ServiceRequest::ReadVersionInfo { envelope, receipt }) => {
                            match auth_bindings
                                .validate_control_cap(&envelope, "read")
                                .map_err(|error| format!("{error:?}"))
                                .and_then(|_| {
                                    authorize(
                                        &coordinator,
                                        &envelope,
                                        &envelope.auth.account_id,
                                        &grants,
                                    )
                                }) {
                                Ok(_) => match coordinator
                                    .read_version(&envelope.action_id, &receipt)
                                    .await
                                {
                                    Ok(bytes) => ServiceResponse::VersionInfo {
                                        content_length: bytes.map(|value| value.len() as u64),
                                    },
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                },
                                Err(error) => ServiceResponse::Error { code: error },
                            }
                        }
                        Ok(ServiceRequest::ReadVersionChunk {
                            envelope,
                            receipt,
                            offset,
                            length,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                            .and_then(|_| {
                                authorize(
                                    &coordinator,
                                    &envelope,
                                    &envelope.auth.account_id,
                                    &grants,
                                )
                            }) {
                            Ok(_) if length as usize > MAX_STAGE_CHUNK => ServiceResponse::Error {
                                code: "version read chunk is too large".into(),
                            },
                            Ok(_) => match coordinator
                                .read_version(&envelope.action_id, &receipt)
                                .await
                            {
                                Ok(None) => ServiceResponse::Chunk {
                                    offset,
                                    content: None,
                                    eof: true,
                                },
                                Ok(Some(bytes)) => {
                                    let offset = offset as usize;
                                    if offset > bytes.len() {
                                        ServiceResponse::Error {
                                            code: "version read offset is outside payload".into(),
                                        }
                                    } else {
                                        let end =
                                            offset.saturating_add(length as usize).min(bytes.len());
                                        ServiceResponse::Chunk {
                                            offset: offset as u64,
                                            content: Some(bytes.slice(offset..end).to_vec()),
                                            eof: end == bytes.len(),
                                        }
                                    }
                                }
                                Err(error) => ServiceResponse::Error {
                                    code: error.to_string(),
                                },
                            },
                            Err(error) => ServiceResponse::Error { code: error },
                        },
                        Ok(ServiceRequest::ListResourceVersions {
                            envelope,
                            resource_id,
                            cursor,
                            limit,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code: error },
                            Ok(_) => match registered_files_resource_key(
                                &resource_id,
                                &envelope.auth,
                                &grants,
                                &resource_map,
                                &host_root,
                                &authorized_roots,
                                true,
                            ) {
                                Err(code) => ServiceResponse::Unavailable {
                                    code: code.into(),
                                    message: if code == "resource_not_found" {
                                        "registered Files resource is missing".into()
                                    } else {
                                        "registered Files resource is no longer active or authorized".into()
                                    },
                                },
                                Ok((resource_key, _handle, _entry)) => match coordinator.list_resource_versions(
                                    &resource_key,
                                    cursor.as_deref(),
                                    limit,
                                ) {
                                    Ok(page) => ServiceResponse::Versions(page),
                                    Err(error) => ServiceResponse::Unavailable {
                                        code: error.code().into(),
                                        message: error.message(),
                                    },
                                },
                            },
                        },
                        Ok(ServiceRequest::ListResourceChanges { envelope, resource_id, cursor, limit }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code: error },
                            Ok(_) => match registered_files_resource_key(&resource_id, &envelope.auth, &grants, &resource_map, &host_root, &authorized_roots, true) {
                                Err(code) => ServiceResponse::Unavailable { code: code.into(), message: "registered Files resource is unavailable or unauthorized".into() },
                                Ok((resource_key, _, _)) => match coordinator.list_resource_changes(&resource_key, cursor.as_deref(), limit) {
                                    Ok(page) => ServiceResponse::Changes(page),
                                    Err(error) => {
                                        let reason = error.to_string();
                                        let code = if reason == "change index incomplete" { "change_index_incomplete" } else if reason == "stale change cursor" { "stale_cursor" } else { "change_query_unavailable" };
                                        ServiceResponse::Unavailable { code: code.into(), message: "Change history is unavailable or needs refresh.".into() }
                                    }
                                },
                            },
                        },
                        Ok(ServiceRequest::GetManagedActionBatch { envelope, request_digest, complete_digest, origin }) => {
                            let result=(|| -> Result<ServiceResponse,String> {
                                auth_bindings.validate_control_cap(&envelope,"read").map_err(|_|"batch unavailable")?;
                                let registry=ResourceRegistry::load_bounded(&resource_map,4*1024*1024).map_err(|_|"batch unavailable")?;
                                let action=coordinator.catalog().get_action(&envelope.action_id).map_err(|_|"batch unavailable")?.ok_or("batch unavailable")?;
                                let members=openclank_history::catalog::managed_batch_members(&action,&envelope.auth.account_id,&request_digest,&complete_digest,&origin).map_err(|_|"batch unavailable")?;
                                let mut projected=Vec::with_capacity(members.len());
                                for (source,before,after) in members {
                                    let (key,_,_)=registered_files_resource_key_in(&source.resource_id,&envelope.auth,&grants,&registry,&host_root,&authorized_roots,true).map_err(|_|"batch unavailable")?;
                                    if key.account_id!=envelope.auth.account_id || key.account_id!=source.account_id || key.workspace_id!=source.workspace_id || key.resource_id!=source.resource_id {return Err("batch unavailable".into());}
                                    projected.push(openclank_history::protocol::ManagedBatchMember{resource_id:key.resource_id,before_version_id:before,after_version_id:after});
                                }
                                Ok(ServiceResponse::ManagedActionBatch(openclank_history::protocol::ManagedActionBatch{version:2,members:projected}))
                            })();
                            result.unwrap_or_else(|_|ServiceResponse::Unavailable{code:"batch_unavailable".into(),message:"Captured batch is unavailable.".into()})
                        },
                        Ok(ServiceRequest::GetManagedActionLocator { envelope, resource_id, origin }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code: error },
                            Ok(_) => match registered_files_resource_key(&resource_id, &envelope.auth, &grants, &resource_map, &host_root, &authorized_roots, true) {
                                Err(code) => ServiceResponse::Unavailable { code: code.into(), message: "registered Files resource is unavailable or unauthorized".into() },
                                Ok((key, _, _)) => match coordinator.get_managed_action_locator(&key,&envelope.action_id,&origin) {
                                    Ok(locator) => ServiceResponse::ManagedActionLocator { locator },
                                    Err(_) => ServiceResponse::Unavailable { code: "change_unavailable".into(), message: "Captured change is unavailable.".into() },
                                },
                            },
                        },
                        Ok(ServiceRequest::GetResourceChange { envelope, resource_id, before_version_id, after_version_id, origin }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code: error },
                            Ok(_) => match registered_files_resource_key(&resource_id, &envelope.auth, &grants, &resource_map, &host_root, &authorized_roots, true) {
                                Err(code) => ServiceResponse::Unavailable { code: code.into(), message: "registered Files resource is unavailable or unauthorized".into() },
                                Ok((key, _, _)) => match coordinator.get_managed_resource_change(&key,&envelope.action_id,&before_version_id,&after_version_id,&origin) {
                                    Ok(change) => ServiceResponse::ResourceChange(change),
                                    Err(_) => ServiceResponse::Unavailable { code: "change_unavailable".into(), message: "Captured change is unavailable.".into() },
                                },
                            },
                        },
                        Ok(ServiceRequest::ListManagedChangeFragments { envelope, resource_id, cursor, limit }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code:error },
                            Ok(_) => match registered_files_resource_key(&resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,true) {
                                Err(code) => ServiceResponse::Unavailable {code:code.into(),message:"Registered resource is unavailable or unauthorized.".into()},
                                Ok((key,_,_)) => match coordinator.list_managed_change_fragments(&key,cursor.as_deref(),limit) {
                                    Ok(page) => ServiceResponse::ManagedChangeFragments(page),
                                    Err(error) => {let reason=error.to_string();let code=if reason=="stale change cursor" {"stale_cursor"} else if reason=="change index incomplete" {"change_index_incomplete"} else {"change_query_unavailable"};ServiceResponse::Unavailable{code:code.into(),message:"Managed change fragments are unavailable.".into()}},
                                },
                            },
                        },
                        Ok(ServiceRequest::CrossOriginFence{..} | ServiceRequest::ListCrossOriginChanges{..}) => ServiceResponse::Unavailable{code:"origin_unavailable".into(),message:"Origin unavailable.".into()},
                        Ok(ServiceRequest::WorkspaceActivityFence{..} | ServiceRequest::ListWorkspaceActivity{..}) => ServiceResponse::Unavailable{code:"workspace_unavailable".into(),message:"Workspace unavailable.".into()},
                        Ok(ServiceRequest::PreviewRegularFileV1{..} | ServiceRequest::ExecuteRegularFileV1{..} | ServiceRequest::GetRegularFileStatusV1{..}) => ServiceResponse::Unavailable{code:"regular_state_unavailable".into(),message:"Regular-file recovery unavailable.".into()},
                        Ok(ServiceRequest::ListOrderedManagedChanges{envelope,resource_id,origin_action_id,cursor,limit}) => match auth_bindings.validate_control_cap(&envelope,"read").map_err(|error|format!("{error:?}")) {
                            Err(code)=>ServiceResponse::Error{code},
                            Ok(_)=>match registered_files_resource_key(&resource_id,&envelope.auth,&grants,&resource_map,&host_root,&authorized_roots,true) {
                                Err(code)=>ServiceResponse::Unavailable{code:code.into(),message:"Registered resource is unavailable or unauthorized.".into()},
                                Ok((key,_,_))=>match coordinator.list_ordered_managed_changes(&key,origin_action_id.as_deref(),cursor.as_deref(),limit) {
                                    Ok(page)=>ServiceResponse::OrderedManagedChanges(page),Err(error)=>ServiceResponse::Unavailable{code:if error.to_string()=="stale change cursor" {"stale_cursor"}else{"ordered_history_unavailable"}.into(),message:"Ordered history is unavailable or needs refresh.".into()},
                                }
                            }
                        },
                        Ok(ServiceRequest::ResolveResourceVersion {
                            envelope,
                            resource_id,
                            version_ref,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code: error },
                            Ok(_) => match registered_files_resource_key(
                                &resource_id,
                                &envelope.auth,
                                &grants,
                                &resource_map,
                                &host_root,
                                &authorized_roots,
                                true,
                            ) {
                                Err(code) => ServiceResponse::Unavailable {
                                    code: code.into(),
                                    message: if code == "resource_not_found" {
                                        "registered Files resource is missing".into()
                                    } else {
                                        "registered Files resource is no longer active or authorized".into()
                                    },
                                },
                                Ok((resource_key, resource, _entry)) => match coordinator
                                    .resolve_resource_version(&resource_key, &version_ref)
                                {
                                    Ok((version, receipt)) => {
                                        ServiceResponse::ResourceVersionSelection {
                                            resource,
                                            version,
                                            receipt,
                                        }
                                    }
                                    Err(error) => ServiceResponse::Unavailable {
                                        code: error.code().into(),
                                        message: error.message(),
                                    },
                                },
                            },
                        },
                        Ok(ServiceRequest::PreviewResourceVersionRestore {
                            envelope,
                            resource_id,
                            version_ref,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "restore")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code: error },
                            Ok(_) => match registered_files_resource_key(
                                &resource_id,
                                &envelope.auth,
                                &grants,
                                &resource_map,
                                &host_root,
                                &authorized_roots,
                                true,
                            ) {
                                Err(code) => ServiceResponse::Unavailable {
                                    code: code.into(),
                                    message: "registered Files resource is missing or unavailable".into(),
                                },
                                Ok((resource_key, _resource, _entry))
                                    if resource_key.account_id != envelope.auth.account_id =>
                                {
                                    ServiceResponse::Unavailable {
                                        code: "resource_unavailable".into(),
                                        message: "restore requires an account-owned Files resource".into(),
                                    }
                                }
                                Ok((resource_key, resource, entry)) => match coordinator
                                    .resolve_resource_version(&resource_key, &version_ref)
                                {
                                    Err(error) => ServiceResponse::Unavailable {
                                        code: error.code().into(),
                                        message: error.message(),
                                    },
                                    Ok((source, _receipt)) => match resolve_registered_path(
                                        &envelope.auth.actor_id,
                                        &entry,
                                        &host_root,
                                        &authorized_roots,
                                    ) {
                                        Err(_) => ServiceResponse::Unavailable {
                                            code: "resource_unavailable".into(),
                                            message: "registered Files destination is unavailable".into(),
                                        },
                                        Ok(destination_path) => {
                                            let provider = FilesystemRestoreProvider::new_with_receipt_root(
                                                &destination_path,
                                                &receipt_root,
                                            );
                                            match provider.current_fingerprint() {
                                                Ok(current) => {
                                                    let destination_exists = current.is_some();
                                                    ServiceResponse::ResourceVersionRestorePreview(
                                                        ResourceVersionRestorePreview {
                                                            resource,
                                                            source,
                                                            destination: RestoreDestinationPreview {
                                                                expected_fingerprint: current
                                                                    .unwrap_or_else(|| "missing".into()),
                                                                exists: destination_exists,
                                                            },
                                                            effect: if destination_exists {
                                                                RestoreEffect::Replace
                                                            } else {
                                                                RestoreEffect::Create
                                                            },
                                                            requires_confirmation: true,
                                                            captures_current_destination: true,
                                                        },
                                                    )
                                                }
                                                Err(_) => ServiceResponse::Unavailable {
                                                    code: "resource_unavailable".into(),
                                                    message: "registered Files destination cannot be read".into(),
                                                },
                                            }
                                        }
                                    },
                                },
                            },
                        },
                        Ok(ServiceRequest::ReadResourceVersionChunk {
                            envelope,
                            resource_id,
                            version_ref,
                            offset,
                            length,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "read")
                            .map_err(|error| format!("{error:?}"))
                        {
                            Err(error) => ServiceResponse::Error { code: error },
                            Ok(_) => match registered_files_resource_key(
                                &resource_id,
                                &envelope.auth,
                                &grants,
                                &resource_map,
                                &host_root,
                                &authorized_roots,
                                true,
                            ) {
                                Err(code) => ServiceResponse::Unavailable {
                                    code: code.into(),
                                    message: if code == "resource_not_found" {
                                        "registered Files resource is missing".into()
                                    } else {
                                        "registered Files resource is no longer active or authorized".into()
                                    },
                                },
                                Ok((resource_key, _handle, _entry)) => match coordinator
                                    .read_resource_version_chunk(
                                        &resource_key,
                                        &version_ref,
                                        offset,
                                        length,
                                    )
                                    .await
                                {
                                    Ok((version, content, eof)) => {
                                        ServiceResponse::ResourceVersionChunk {
                                            version,
                                            offset,
                                            content,
                                            eof,
                                        }
                                    }
                                    Err(error) => ServiceResponse::Unavailable {
                                        code: error.code().into(),
                                        message: error.message(),
                                    },
                                },
                            },
                        },
                        Ok(ServiceRequest::LookupManagedAction { envelope, operation_id }) => {
                            match auth_bindings.validate_control_cap(&envelope, "capture") {
                                Err(error) => ServiceResponse::Error { code:format!("{error:?}") },
                                Ok(_) if operation_id.len() < 16 || operation_id.len() > 80 || !operation_id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_') => ServiceResponse::Error { code:"invalid managed operation identity".into() },
                                Ok(_) => {
                                    let binding = format!("{}\0{}\0{}", envelope.auth.account_id, envelope.auth.actor_id, operation_id);
                                    let expected = format!("odysseus-managed-{:x}", Sha256::digest(binding.as_bytes()));
                                    if envelope.action_id != expected {
                                        ServiceResponse::Error { code:"managed action identity mismatch".into() }
                                    } else {
                                        match coordinator.catalog().get_action(&expected) {
                                            Ok(None) => ServiceResponse::Unavailable { code:"managed_action_not_found".into(), message:"No action was recorded for this operation.".into() },
                                            Ok(Some(_)) => match authorize(&coordinator, &envelope, &envelope.auth.account_id, &grants) {
                                                Ok(record) => ServiceResponse::Action(record),
                                                Err(error) => ServiceResponse::Error { code:error },
                                            },
                                            Err(_) => ServiceResponse::Error { code:"managed action lookup unavailable".into() },
                                        }
                                    }
                                }
                            }
                        }
                        Ok(ServiceRequest::SettleRecovery(envelope)) => {
                            let action = auth_bindings.validate_control_cap(&envelope, "capture")
                                .map_err(|e| format!("{e:?}")).and_then(|_| authorize(&coordinator,
                                    &envelope, &envelope.auth.account_id, &grants));
                            match action {
                                Ok(record) => match coordinator.settle_recovery(record).await {
                                    Ok(record) => ServiceResponse::Action(record),
                                    Err(error) => ServiceResponse::Error { code: error.to_string() },
                                },
                                Err(code) => ServiceResponse::Error { code },
                            }
                        }
                        Ok(ServiceRequest::GetCaptureAction(envelope)) => {
                            match auth_bindings.validate_control_cap(&envelope, "read")
                                .or_else(|_| auth_bindings.validate_control_cap(&envelope, "capture"))
                                .map_err(|error| format!("{error:?}"))
                                .and_then(|_| authorize(&coordinator, &envelope, &envelope.auth.account_id, &grants)) {
                                Ok(record) => ServiceResponse::Action(record),
                                Err(error) => ServiceResponse::Error { code: error },
                            }
                        }
                        Ok(ServiceRequest::AfterUnavailable(envelope)) => {
                            match auth_bindings.validate_control_cap(&envelope, "capture")
                                .map_err(|error| format!("{error:?}"))
                                .and_then(|_| authorize(&coordinator, &envelope, &envelope.auth.account_id, &grants)) {
                                Ok(_) => match coordinator.after_unavailable(&envelope.action_id) {
                                    Ok(record) => ServiceResponse::Action(record),
                                    Err(error) => ServiceResponse::Error { code: error.to_string() },
                                },
                                Err(error) => ServiceResponse::Error { code: error },
                            }
                        }
                        Ok(ServiceRequest::Reconcile { envelope, receipt }) => {
                            match auth_bindings
                                .validate_control_cap(&envelope, "capture")
                                .map_err(|error| format!("{error:?}"))
                                .and_then(|_| {
                                    authorize(
                                        &coordinator,
                                        &envelope,
                                        &envelope.auth.account_id,
                                        &grants,
                                    )
                                }) {
                                Ok(_) => {
                                    match coordinator.reconcile_one(&envelope.action_id, receipt) {
                                        Ok(record) => ServiceResponse::Action(record),
                                        Err(error) => ServiceResponse::Error {
                                            code: error.to_string(),
                                        },
                                    }
                                }
                                Err(error) => ServiceResponse::Error { code: error },
                            }
                        }
                        Ok(ServiceRequest::PutMemeryComponent { envelope, change, digest, size, upload_id, policy_revision, resource, relative_path }) => {
                            let result = async {
                                auth_bindings.validate_memery(&envelope).map_err(|e| format!("{e:?}"))?;
                                if size > openclank_history::memery_components::MAX_SIZE as u64
                                    || envelope.action_id != format!("memery-component-{change}-{digest}") {
                                    return Err("invalid Memery component".to_string());
                                }
                                let owned = {
                                    let uploads = staged_uploads.lock().await;
                                    let entry = uploads.get(&upload_id).ok_or("component stage missing")?;
                                    OwnedStagedUpload { upload_id: upload_id.clone(), io_lock: entry.io_lock.clone() }
                                };
                                let bytes = read_owned_staged_upload(&staged_uploads, &owned, &envelope.auth.actor_id,
                                    &envelope.auth.account_id, &envelope.action_id, &format!("sha256:{digest}:{size}")).await?;
                                staging_budget_error(&coordinator, &staging_root, size.saturating_add(16384),
                                    staged_bytes.load(std::sync::atomic::Ordering::Acquire))?;
                                let result = coordinator.put_memery_component(&envelope.auth.account_id,
                                    &change, &digest, bytes, policy_revision, &resource, &relative_path).await.map_err(|e| e.to_string());
                                remove_owned_staged_uploads(&staged_uploads, &staged_bytes, &envelope.auth.actor_id,
                                    &envelope.auth.account_id, &envelope.action_id, &[owned]).await;
                                result
                            }.await;
                            match result { Ok(()) => ServiceResponse::Accepted, Err(code) => ServiceResponse::Error { code } }
                        }
                        Ok(ServiceRequest::InspectMemeryComponent { envelope, change, digest, resource }) => {
                            match auth_bindings.validate_memery(&envelope).map_err(|e| format!("{e:?}"))
                                .and_then(|_| coordinator.memery_component_location(&envelope.auth.account_id,
                                    &change, &digest, &resource).map_err(|e| e.to_string())) {
                                Ok(path) => ServiceResponse::MemeryComponentLocation(path),
                                Err(code) => ServiceResponse::Error { code },
                            }
                        }
                        Ok(ServiceRequest::ReadMemeryComponent { envelope, change, digest, resource, part }) => {
                            let result = async {
                                auth_bindings.validate_memery(&envelope).map_err(|e| format!("{e:?}"))?;
                                coordinator.read_memery_component(&envelope.auth.account_id, &change, &digest, &resource, part)
                                    .await.map_err(|e| e.to_string())
                            }.await;
                            match result { Ok(bytes) => ServiceResponse::Bytes(bytes), Err(code) => ServiceResponse::Error { code } }
                        }
                        Ok(ServiceRequest::SweepMemeryComponents(envelope)) => {
                            let result = async {
                                auth_bindings.validate_memery(&envelope).map_err(|e| format!("{e:?}"))?;
                                coordinator.sweep_memery_components(&envelope.auth.account_id).await.map_err(|e| e.to_string())
                            }.await;
                            match result { Ok(()) => ServiceResponse::Accepted, Err(code) => ServiceResponse::Error { code } }
                        }
                        Ok(ServiceRequest::GetMemeryManifest { envelope, assets }) => {
                            match auth_bindings.validate_memery(&envelope)
                                .map_err(|err| format!("{err:?}"))
                                .and_then(|_| coordinator.catalog().memery_manifest(&envelope.auth.account_id, assets)
                                    .map_err(|err| err.to_string())) {
                                Ok(state) => ServiceResponse::MemeryManifest(state),
                                Err(code) => ServiceResponse::Error { code },
                            }
                        }
                        Ok(ServiceRequest::SettleMemeryManifest { envelope, assets, checkpoint, generation, position, witness, add, remove }) => {
                            match auth_bindings.validate_memery(&envelope)
                                .map_err(|err| format!("{err:?}"))
                                .and_then(|_| {
                                    if !add.is_empty() {
                                        let bytes = serde_json::to_vec(&add).map_err(|e| e.to_string())?.len() as u64;
                                        staging_budget_error(&coordinator, &staging_root, bytes.saturating_add(8192),
                                            staged_bytes.load(std::sync::atomic::Ordering::Acquire))?;
                                    }
                                    coordinator.catalog().settle_memery(&envelope.auth.account_id, assets, checkpoint,
                                        generation, position, witness, add, remove).map_err(|err| err.to_string())
                                }) {
                                Ok(state) => ServiceResponse::MemeryManifest(state),
                                Err(code) => ServiceResponse::Error { code },
                            }
                        }
                        Ok(ServiceRequest::GetCapturePolicy(envelope)) => {
                            match auth_bindings.validate_control_cap(&envelope, "capture")
                                .or_else(|_| auth_bindings.validate_control_cap(&envelope, "settings-read")) {
                                Ok(_) => match coordinator.policy_set() {
                                    Ok(policy) => ServiceResponse::CapturePolicy {
                                        capture: policy.capture_for(&envelope.auth.account_id), revision: policy.revision,
                                    },
                                    Err(error) => ServiceResponse::Error { code: error.to_string() },
                                },
                                Err(error) => ServiceResponse::Error { code: format!("{error:?}") },
                            }
                        }
                        Ok(ServiceRequest::GetPolicy(envelope)) => {
                            match auth_bindings.validate_control_cap(&envelope, "settings-read") {
                                Ok(binding) => match coordinator.policy_set() {
                                    Ok(mut policy) => {
                                        if !(binding.capabilities.contains("admin") || binding.capabilities.contains("settings-admin")) {
                                            policy.accounts.retain(|id, _| id == &envelope.auth.account_id);
                                            policy.scopes.retain(|scope| {
                                                scope.owner_account_id.as_deref()
                                                    == Some(envelope.auth.account_id.as_str())
                                            });
                                        }
                                        ServiceResponse::Policy(policy)
                                    }
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                },
                                Err(error) => ServiceResponse::Error {
                                    code: format!("{error:?}"),
                                },
                            }
                        }
                        Ok(ServiceRequest::SetPolicy {
                            envelope,
                            expected_revision,
                            policy,
                        }) => match auth_bindings.validate_control_cap(&envelope, "settings-write")
                        {
                            Ok(binding) => {
                                let update = coordinator.policy_set().and_then(|current| {
                                    if binding.capabilities.contains("admin") || binding.capabilities.contains("settings-admin") {
                                        return Ok(policy);
                                    }
                                    if policy.global != current.global || policy.capture_defaults != current.capture_defaults
                                        || policy.accounts.keys().any(|id| id != &envelope.auth.account_id)
                                        || policy.scopes.iter().any(|scope| {
                                            scope.kind == openclank_history::retention::ScopeKind::Global ||
                                            scope.owner_account_id.as_deref()
                                                != Some(envelope.auth.account_id.as_str())
                                        })
                                    {
                                        return Err(std::io::Error::new(
                                            std::io::ErrorKind::PermissionDenied,
                                            "account cannot modify global or another account's history settings",
                                        ).into());
                                    }
                                    let mut merged = current;
                                    merged.scopes.retain(|scope| {
                                        scope.owner_account_id.as_deref()
                                            != Some(envelope.auth.account_id.as_str())
                                    });
                                    merged.scopes.extend(policy.scopes);
                                    merged.accounts.remove(&envelope.auth.account_id);
                                    merged.accounts.extend(policy.accounts);
                                    Ok(merged)
                                });
                                match update.and_then(|next| {
                                    coordinator.set_policy(expected_revision, next)
                                }) {
                                    Ok(mut policy) => {
                                        if !(binding.capabilities.contains("admin") || binding.capabilities.contains("settings-admin")) {
                                            policy.accounts.retain(|id, _| id == &envelope.auth.account_id);
                                            policy.scopes.retain(|scope| scope.owner_account_id.as_deref() == Some(envelope.auth.account_id.as_str()));
                                        }
                                        ServiceResponse::Policy(policy)
                                    },
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                }
                            }
                            Err(error) => ServiceResponse::Error {
                                code: format!("{error:?}"),
                            },
                        },
                        Ok(ServiceRequest::GetUsage(envelope)) => {
                            match auth_bindings.validate_control_cap(&envelope, "settings-read") {
                                Ok(binding) => {
                                    match (coordinator.usage(), coordinator.policy_set()) {
                                        (Ok(mut usage), Ok(policy)) => {
                                            usage.reserved_inflight_bytes =
                                                usage.reserved_inflight_bytes.saturating_add(
                                                    staged_bytes.load(Ordering::Acquire),
                                                );
                                            if !(binding.capabilities.contains("admin") || binding.capabilities.contains("settings-admin")) {
                                                let visible = policy
                                                    .scopes
                                                    .iter()
                                                    .filter(|scope| {
                                                        scope.owner_account_id.as_deref()
                                                            == Some(
                                                                envelope.auth.account_id.as_str(),
                                                            )
                                                    })
                                                    .map(|scope| scope.scope_id.clone())
                                                    .collect::<BTreeSet<_>>();
                                                usage.scopes.retain(|scope| {
                                                    visible.contains(&scope.scope_id)
                                                });
                                            }
                                            ServiceResponse::Usage(usage)
                                        }
                                        (Err(error), _) | (_, Err(error)) => {
                                            ServiceResponse::Error {
                                                code: error.to_string(),
                                            }
                                        }
                                    }
                                }
                                Err(error) => ServiceResponse::Error {
                                    code: format!("{error:?}"),
                                },
                            }
                        }
                        Ok(ServiceRequest::GetStatus(envelope)) => {
                            match auth_bindings.validate_control_cap(&envelope, "settings-read") {
                                Ok(_) => match coordinator.status() {
                                    Ok((mut state, mut reason, mut history_paused)) => {
                                        let staged = staged_bytes.load(Ordering::Acquire);
                                        if !history_paused
                                            && coordinator
                                                .policy_set()
                                                .ok()
                                                .zip(coordinator.usage().ok())
                                                .is_some_and(|(policy, usage)| {
                                                    usage
                                                        .physical_allocated_bytes
                                                        .saturating_add(
                                                            usage.reserved_inflight_bytes,
                                                        )
                                                        .saturating_add(staged)
                                                        >= policy.global.total_bytes
                                                })
                                        {
                                            state = "history_paused_budget".into();
                                            reason = Some(
                                                "history budget is full, including in-flight staged uploads"
                                                    .into(),
                                            );
                                            history_paused = true;
                                        }
                                        if !history_paused && coordinator.policy_set().is_ok_and(|policy|
                                            !policy.capture_for(&envelope.auth.account_id).enabled) {
                                            history_paused = true;
                                            state = "history_paused_disabled".into();
                                            reason = Some("Automatic history is off for this account".into());
                                        }
                                        ServiceResponse::Status {
                                            state,
                                            reason,
                                            history_paused,
                                            last_capture: coordinator.catalog().last_capture_status(&envelope.auth.account_id).ok().flatten(),
                                            staged_bytes: staged,
                                        }
                                    }
                                    Err(error) => ServiceResponse::Error {
                                        code: error.to_string(),
                                    },
                                },
                                Err(error) => ServiceResponse::Error {
                                    code: format!("{error:?}"),
                                },
                            }
                        }
                        Ok(ServiceRequest::GetRestoreStatus {
                            envelope, restore_id, resource_id, source_action_id, source_version_id,
                            expected_destination_fingerprint,
                        }) => {
                            let unavailable = || ServiceResponse::Unavailable {
                                code: "restore_status_unavailable".into(),
                                message: "Restore status is unavailable.".into(),
                            };
                            let valid = [&restore_id, &resource_id, &source_action_id, &source_version_id, &expected_destination_fingerprint]
                                .iter().all(|v| !v.is_empty() && v.len() <= 1024)
                                && restore_id.len() <= 256 && envelope.action_id == restore_id;
                            if !valid || auth_bindings.validate_control_cap(&envelope, "read").is_err() {
                                unavailable()
                            } else if let Ok((key, _, _)) = registered_files_resource_key(
                                &resource_id, &envelope.auth, &grants, &resource_map, &host_root, &authorized_roots, true,
                            ) {
                                if key.account_id != envelope.auth.account_id {
                                    unavailable()
                                } else {
                                    match coordinator.catalog().get_restore::<openclank_history::restore::RestoreJournal>(&restore_id) {
                                        Ok(None) => ServiceResponse::RestoreStatus { state:"unknown".into(), outcome:None, verified:false },
                                        Ok(Some(journal)) if journal.restore_id == restore_id && journal.destination == key
                                            && journal.source_action_id == source_action_id && journal.source_version_id == source_version_id
                                            && journal.expected_destination_fingerprint.as_deref() == if expected_destination_fingerprint == "missing" { None } else { Some(expected_destination_fingerprint.as_str()) } => {
                                            use openclank_history::restore::{RestoreOutcome, RestoreState};
                                            let state = match journal.state {
                                                RestoreState::Prepared | RestoreState::CurrentCaptured | RestoreState::Applying => "pending",
                                                RestoreState::Complete if journal.outcome == Some(RestoreOutcome::Complete) => "complete",
                                                RestoreState::Conflict => "conflict",
                                                RestoreState::Partial => "partial",
                                                _ => "unknown",
                                            };
                                            let verified = journal.verification.as_ref().is_some_and(|proof|
                                                proof.restore_id == restore_id && proof.resource_id == key.resource_id
                                                && proof.version_id == source_version_id && proof.status == "Verified"
                                                && proof.restored_content_hash.as_deref() == Some(proof.content_hash.as_str()));
                                            ServiceResponse::RestoreStatus { state:state.into(), outcome:journal.outcome, verified }
                                        },
                                        _ => unavailable(),
                                    }
                                }
                            } else { unavailable() }
                        }
                        Ok(ServiceRequest::RestoreResourceVersion {
                            envelope,
                            restore_id,
                            resource_id,
                            version_ref,
                            expected_destination_fingerprint,
                        }) => {
                            restore_registered_resource_version(
                                &coordinator,
                                envelope,
                                restore_id,
                                resource_id,
                                version_ref,
                                expected_destination_fingerprint,
                                &auth_bindings,
                                &grants,
                                &resource_map,
                                &host_root,
                                &receipt_root,
                                &authorized_roots,
                            )
                            .await
                        }
                        Ok(ServiceRequest::RestoreHost {
                            envelope,
                            request,
                            source,
                            destination_path,
                            source_host_metadata,
                        }) => {
                            let authorized = auth_bindings.validate_control_cap(&envelope, "restore")
                                .and_then(|_| {
                                    if request.account_id != envelope.auth.account_id
                                        || request.destination.account_id != envelope.auth.account_id
                                        || request.source_action_id.is_empty()
                                        || request.source_version_id != source.version_id
                                    {
                                        return Err(openclank_history::protocol::ProtocolError::AccountMismatch);
                                    }
                                    Ok(())
                                })
                                .and_then(|_| {
                                    authorize(
                                        &coordinator,
                                        &ControlEnvelope {
                                            protocol_version: envelope.protocol_version,
                                            auth: envelope.auth.clone(),
                                            action_id: request.source_action_id.clone(),
                                        },
                                        &envelope.auth.account_id,
                                        &grants,
                                    )
                                    .map(|_| ())
                                    .map_err(|_| openclank_history::protocol::ProtocolError::AccountMismatch)
                                });
                            match authorized {
                                Ok(()) => {
                                    let candidate = std::path::Path::new(&destination_path);
                                    let resolved = candidate
                                        .parent()
                                        .and_then(|parent| parent.canonicalize().ok())
                                        .and_then(|parent| {
                                            candidate.file_name().map(|name| parent.join(name))
                                        });
                                    let registered_path = ResourceRegistry::load(&resource_map)
                                        .ok()
                                        .and_then(|registry| {
                                            let entry = registry
                                                .entries
                                                .get(&request.destination.resource_id)?;
                                            if !entry.active
                                                || entry.account_id
                                                    != request.destination.account_id
                                                || entry.workspace_id
                                                    != request.destination.workspace_id
                                                || !owner_allowed(
                                                    &entry.account_id,
                                                    &envelope.auth.actor_id,
                                                    &envelope.auth.account_id,
                                                    &grants,
                                                )
                                            {
                                                return None;
                                            }
                                            resolve_registered_path(
                                                &envelope.auth.actor_id,
                                                entry,
                                                &host_root,
                                                &authorized_roots,
                                            )
                                            .ok()
                                        });
                                    if resolved.is_none()
                                        || registered_path.is_none()
                                        || resolved.as_ref().unwrap()
                                            != registered_path.as_ref().unwrap()
                                    {
                                        ServiceResponse::Error { code: "restore destination is not the registered provider resource".into() }
                                    } else {
                                        let destination = resolved.unwrap();
                                        let mut provider =
                                            FilesystemRestoreProvider::new_with_receipt_root(
                                                &destination,
                                                &receipt_root,
                                            );
                                        let observed =
                                            provider.current_fingerprint().ok().flatten();
                                        let prepared = prepare_restore_authorized(
                                            &coordinator,
                                            &request,
                                            &source,
                                            observed.as_deref(),
                                        )
                                        .await;
                                        match prepared {
                                            Ok(mut plan) => {
                                                plan.source_host_metadata = source_host_metadata;
                                                match apply_restore_authorized_durable(
                                                    &coordinator,
                                                    &request,
                                                    &plan,
                                                    &mut provider,
                                                )
                                                .await
                                                {
                                                    Ok(receipt) => {
                                                        ServiceResponse::Restore(receipt)
                                                    }
                                                    Err(outcome) => ServiceResponse::Error {
                                                        code: format!(
                                                            "restore outcome: {outcome:?}"
                                                        ),
                                                    },
                                                }
                                            }
                                            Err(outcome) => ServiceResponse::Error {
                                                code: format!("restore outcome: {outcome:?}"),
                                            },
                                        }
                                    }
                                }
                                Err(error) => ServiceResponse::Error {
                                    code: format!("{error:?}"),
                                },
                            }
                        }
                        Err(error) => ServiceResponse::Error {
                            code: error.to_string(),
                        },
                    }
                };
                let encoded = match serde_json::to_string(&response) {
                    Ok(value) => value,
                    Err(_) => break,
                };
                if write.write_all(encoded.as_bytes()).await.is_err() {
                    break;
                }
                if write.write_all(b"\n").await.is_err() {
                    break;
                }
            }
        });
    }
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicU64;

    fn registry_with(active: bool) -> ResourceRegistry {
        let mut registry = ResourceRegistry::default();
        registry.entries.insert("file:r".into(), ResourceRegistration {
            resource_id: "file:r".into(), actor_ids: Default::default(), account_id: "account".into(), workspace_id: "ws".into(),
            root_id: "root".into(), root_path: "/".into(), relative_path: "x".into(), active, generation: 1, updated_millis: 0 });
        registry
    }

    #[test]
    fn a_removed_registration_is_found_only_when_inactive_is_allowed() {
        let auth = openclank_history::protocol::AuthContext { actor_id: "actor".into(), account_id: "account".into(), token: "t".into() };
        let root: [AuthorizedHistoryRoot; 0] = [];
        let dir = std::path::Path::new("/");
        let find = |active, allow| registered_files_resource_key_in("file:r", &auth, &[], &registry_with(active), dir, &root, allow).is_ok();
        assert!(find(true, false) && find(true, true), "an active registration is unchanged");
        assert!(!find(false, false), "a removed registration is still refused by the default");
        assert!(find(false, true), "and found when the caller allows removed ones");
    }

    #[test]
    fn a_removed_registration_restores_only_into_an_absent_leaf() {
        use openclank_history::restore::{RegularFileObservationV1, RegularFileStateV1};
        let seen = |state| RegularFileObservationV1 { state, parent_device: 1, parent_inode: 2, file_device: None, file_inode: None };
        let present = seen(RegularFileStateV1::Present { sha256: "a".repeat(64), size: 1, mode: 0o644 });
        let absent = seen(RegularFileStateV1::Absent);
        assert!(regular_restore_state_allowed(true, &present) && regular_restore_state_allowed(true, &absent));
        assert!(regular_restore_state_allowed(false, &absent));
        assert!(!regular_restore_state_allowed(false, &present), "nothing present is replaced through a removed registration");
    }

    fn staged_upload(
        action_id: &str,
        path: std::path::PathBuf,
        io_lock: std::sync::Arc<tokio::sync::Mutex<()>>,
    ) -> StagedUpload {
        StagedUpload {
            read_file:None, read_version_id:None, read_digest:None, worktree_completion_issued:false,
            checkpoint_slot_not_started: false,
                                                stage_file_ready: true,
            actor_id: "actor".into(),
            account_id: "account".into(),
            action_id: action_id.into(),
            content_length: 1,
            written: 1,
            fingerprint: "sha256:00:1".into(),
            finished: true,
            last_activity_millis: 0,
            path,
            io_lock,
            io_active_count: std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0)),
        }
    }

    #[test]
    fn completed_worktree_upload_reaps_its_owned_bytes() {
        let root=tempfile::tempdir().unwrap();let path=root.path().join("payload");std::fs::write(&path,b"x").unwrap();
        let mut upload=staged_upload("action",path.clone(),std::sync::Arc::new(tokio::sync::Mutex::new(())));upload.worktree_completion_issued=true;
        let mut uploads=BTreeMap::from([("odysseus-worktree-fixture".to_owned(),upload)]);let bytes=std::sync::atomic::AtomicU64::new(1);
        reap_staged_uploads(&mut uploads,2,1,&bytes);assert!(uploads.is_empty());assert!(!path.exists());assert_eq!(bytes.load(std::sync::atomic::Ordering::Acquire),0);
    }

    #[tokio::test]
    async fn cleanup_does_not_remove_stage_reusing_captured_upload_id() {
        let root = tempfile::tempdir().unwrap();
        let old_path = root.path().join("old");
        let new_path = root.path().join("new");
        std::fs::write(&old_path, b"o").unwrap();
        std::fs::write(&new_path, b"n").unwrap();
        let old_lock = std::sync::Arc::new(tokio::sync::Mutex::new(()));
        let new_lock = std::sync::Arc::new(tokio::sync::Mutex::new(()));
        let uploads = std::sync::Arc::new(tokio::sync::Mutex::new(BTreeMap::from([(
            "U".to_owned(),
            staged_upload("action", old_path.clone(), old_lock.clone()),
        )])));
        let captured = owned_staged_ids_for_entries(
            &uploads,
            &[BatchPrepareEntry {
                resource_key: openclank_history::catalog::ResourceKey {
                    account_id: "account".into(),
                    workspace_id: "workspace".into(),
                    provider: "provider".into(),
                    resource_id: "resource".into(),
                },
                old_locator: Some("old".into()),
                new_locator: Some("new".into()),
                expected_revision: None,
                existence: openclank_history::catalog::ResourceExistence::Present,
                resource_type: openclank_history::catalog::ResourceType::File,
                metadata: openclank_history::catalog::ResourceMetadata::default(),
                content: None,
                staged_upload_id: Some("U".into()),
                fingerprint: "sha256:00:1".into(),
                coverage: Default::default(),
            }],
            "actor",
            "account",
            "action",
        )
        .await;
        assert_eq!(captured.len(), 1);
        let replacement = staged_upload("action", new_path.clone(), new_lock);
        uploads.lock().await.insert("U".into(), replacement);
        let staged_bytes = AtomicU64::new(2);
        remove_owned_staged_uploads(
            &uploads,
            &staged_bytes,
            "actor",
            "account",
            "action",
            &captured,
        )
        .await;
        assert!(uploads.lock().await.contains_key("U"));
        assert!(new_path.exists());
        assert!(old_path.exists());
        assert_eq!(staged_bytes.load(std::sync::atomic::Ordering::Acquire), 2);
    }
}
