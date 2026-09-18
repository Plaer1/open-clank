//! Unix single-writer history service boundary. Windows transport is unsupported here.

#[cfg(unix)]
use openclank_history::catalog::{ActionRecord, ActionState, BeginResult};
#[cfg(unix)]
use openclank_history::operations::{HistoryCoordinator, begin, capture_input_digest};
#[cfg(unix)]
use openclank_history::protocol::{
    ControlEnvelope, ProtocolError, RequestEnvelope, ResourceHandle, ServiceRequest,
    ServiceResponse, validate as protocol_validate, validate_control as protocol_validate_control,
};
#[cfg(unix)]
use openclank_history::registry::{ResourceRegistration, ResourceRegistry};
#[cfg(unix)]
use openclank_history::restore::{
    FilesystemRestoreProvider, RestoreProvider, apply_restore_authorized_durable,
    prepare_restore_authorized,
};
#[cfg(unix)]
use sha2::{Digest, Sha256};
#[cfg(unix)]
use std::collections::{BTreeMap, BTreeSet};

#[cfg(unix)]
const MAX_FRAME: usize = 1024 * 1024;
#[cfg(unix)]
const MAX_STAGE_CHUNK: usize = 512 * 1024;
/// Staging is a disk-safety reservation, not a per-file eligibility limit. A
/// provider may upload any size that its configured history budget and the
/// available staging reservation can admit. Operators can tune this bound for
/// a small disk with the environment setting; it is deliberately much larger
/// than the IPC frame size.
#[cfg(unix)]
const DEFAULT_STAGING_QUOTA_BYTES: u64 = 2 * 1024 * 1024 * 1024;
#[cfg(unix)]
const DEFAULT_STAGING_TTL_MILLIS: u64 = 15 * 60 * 1000;
#[cfg(unix)]
const DEFAULT_MAX_ACTIVE_STAGES: usize = 64;

#[cfg(unix)]
#[derive(Clone, Debug, serde::Deserialize)]
struct ServerCredential {
    actor_id: String,
    account_id: String,
    token: String,
    #[serde(default)]
    owner_accounts: BTreeSet<String>,
    #[serde(default)]
    capabilities: BTreeSet<String>,
}

/// Provider roots are assembled by the authenticated app/Files supervisor
/// from its root registry and passed to this process at startup. A request may
/// name only one of these root ids; the path is checked against the same record
/// before it enters the service-owned resource map.
#[cfg(unix)]
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
}

#[cfg(unix)]
fn default_root_kind() -> String {
    "recursive_directory".into()
}

#[cfg(unix)]
fn root_is_exact(root: &AuthorizedHistoryRoot) -> bool {
    root.kind == "exact_file"
}

#[cfg(unix)]
struct StagedIoPermit {
    active: std::sync::Arc<std::sync::atomic::AtomicUsize>,
}

#[cfg(unix)]
impl Drop for StagedIoPermit {
    fn drop(&mut self) {
        self.active
            .fetch_sub(1, std::sync::atomic::Ordering::AcqRel);
    }
}

#[cfg(unix)]
#[derive(Clone, Debug)]
struct StagedUpload {
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

#[cfg(unix)]
#[derive(Clone, Debug)]
struct AuthBindings {
    credentials: Vec<ServerCredential>,
    legacy_account: Option<String>,
    legacy_token: Option<String>,
}

#[cfg(unix)]
#[derive(Clone, Debug, serde::Deserialize)]
struct AuthorityManifest {
    generation: u64,
    credentials: Vec<ServerCredential>,
    #[serde(default)]
    authorized_roots: Vec<AuthorizedHistoryRoot>,
}

#[cfg(unix)]
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
            return (previous.into_iter().collect(), Vec::new());
        };
        let _generation = manifest.generation;
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
            });
        }
        Err(ProtocolError::InvalidToken)
    }

    fn validate_request(
        &self,
        envelope: &RequestEnvelope,
    ) -> Result<ServerCredential, ProtocolError> {
        let binding = self.authenticate(&envelope.auth)?;
        protocol_validate(envelope, &binding.account_id, &binding.token)?;
        Ok(binding)
    }

    fn validate_control(
        &self,
        envelope: &ControlEnvelope,
    ) -> Result<ServerCredential, ProtocolError> {
        let binding = self.authenticate(&envelope.auth)?;
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

    fn validate_control_cap(
        &self,
        envelope: &ControlEnvelope,
        capability: &str,
    ) -> Result<ServerCredential, ProtocolError> {
        let binding = self.validate_control(envelope)?;
        Self::require(&binding, capability)?;
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

#[cfg(unix)]
fn valid_upload_id(upload_id: &str) -> bool {
    upload_id.len() <= 128
        && !upload_id.is_empty()
        && !upload_id
            .chars()
            .any(|character| character.is_whitespace() || matches!(character, '/' | '\\' | '\0'))
}

#[cfg(unix)]
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

#[cfg(unix)]
fn staged_upload_path(root: &std::path::Path, upload_id: &str) -> std::path::PathBuf {
    root.join(format!(".openclank-stage-{upload_id}.part"))
}

#[cfg(unix)]
fn remove_staged_upload(upload: &StagedUpload, staged_bytes: &std::sync::atomic::AtomicU64) {
    let _ = std::fs::remove_file(&upload.path);
    staged_bytes.fetch_sub(upload.content_length, std::sync::atomic::Ordering::AcqRel);
}

#[cfg(unix)]
fn reap_staged_uploads(
    uploads: &mut BTreeMap<String, StagedUpload>,
    now_millis: u64,
    ttl_millis: u64,
    staged_bytes: &std::sync::atomic::AtomicU64,
) {
    let stale = uploads
        .iter()
        .filter(|(_, upload)| {
            now_millis.saturating_sub(upload.last_activity_millis) >= ttl_millis
                && upload
                    .io_active_count
                    .load(std::sync::atomic::Ordering::Acquire)
                    == 0
        })
        .map(|(upload_id, _)| upload_id.clone())
        .collect::<Vec<_>>();
    for upload_id in stale {
        if let Some(upload) = uploads.remove(&upload_id) {
            remove_staged_upload(&upload, staged_bytes);
        }
    }
}

#[cfg(unix)]
fn verify_staged_upload(upload: &StagedUpload) -> Result<(), String> {
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

#[cfg(unix)]
fn available_staging_bytes(path: &std::path::Path) -> Option<u64> {
    let path = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).ok()?;
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // `path` is a NUL-free, service-owned staging directory and `stats` is
    // initialized only after the OS reports success.
    let result = unsafe { libc::statvfs(path.as_ptr(), stats.as_mut_ptr()) };
    if result != 0 {
        return None;
    }
    let stats = unsafe { stats.assume_init() };
    (stats.f_bavail as u64).checked_mul(stats.f_frsize as u64)
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

#[cfg(unix)]
fn staging_budget_error(
    coordinator: &HistoryCoordinator,
    staging_root: &std::path::Path,
    requested: u64,
    currently_staged: u64,
) -> Result<(), String> {
    let policy = coordinator
        .policy_set()
        .map_err(|error| format!("history budget could not be read: {error}"))?;
    if !policy.global.enabled {
        return Err("history_paused_disabled: history capture is disabled".into());
    }
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
    let free = available_staging_bytes(staging_root).unwrap_or(u64::MAX);
    let free_for_stage = free.saturating_sub(64 * 1024 * 1024);
    if requested > free_for_stage {
        return Err(format!(
            "history_paused_disk: staged payload exceeds available staging capacity ({free_for_stage} bytes available)"
        ));
    }
    Ok(())
}

#[cfg(unix)]
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
                .validate_control_cap(&envelope, "capture")
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
                                            use std::os::unix::fs::PermissionsExt;
                                            let _ = file.set_permissions(
                                                std::fs::Permissions::from_mode(0o600),
                                            );
                                        }
                                        let now = registry_timestamp();
                                        uploads.insert(
                                            upload_id,
                                            StagedUpload {
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
                .validate_control_cap(&envelope, "capture")
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
                                Some(upload) if upload.finished || offset != upload.written => {
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
                .validate_control_cap(&envelope, "capture")
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
                        Some(upload) if upload.written != upload.content_length => {
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
            let response = match auth_bindings
                .validate_control_cap(&envelope, "capture")
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

#[cfg(unix)]
struct SocketGuard {
    socket: String,
    lock: String,
}
#[cfg(unix)]
impl Drop for SocketGuard {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.socket);
        let _ = std::fs::remove_file(&self.lock);
    }
}

#[cfg(unix)]
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

#[cfg(unix)]
fn owner_allowed(owner: &str, actor: &str, account: &str, grants: &[(String, String)]) -> bool {
    owner == account
        || grants
            .iter()
            .any(|(granted_actor, granted_owner)| granted_actor == actor && granted_owner == owner)
}

#[cfg(unix)]
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

#[cfg(unix)]
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

#[cfg(unix)]
fn valid_resource_id(resource_id: &str) -> bool {
    resource_id.starts_with("file:")
        && resource_id.len() <= 256
        && !resource_id
            .chars()
            .any(|character| character.is_whitespace() || matches!(character, '/' | '\\' | '\0'))
}

#[cfg(unix)]
fn registry_timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[cfg(unix)]
fn registry_registration(
    registry: &ResourceRegistry,
    resource_id: &str,
    account_id: &str,
    workspace_id: &str,
    root_id: Option<&str>,
    root_path: &str,
    relative_path: &str,
    host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<(ResourceRegistration, bool), String> {
    if account_id.is_empty() || workspace_id.is_empty() {
        return Err("resource registration identity is incomplete".into());
    }
    if !ResourceRegistry::validate_relative_path(relative_path) {
        return Err("resource registration path is invalid".into());
    }
    let (root_id, root_path) = registry_destination(
        account_id,
        workspace_id,
        root_id,
        root_path,
        relative_path,
        host_root,
        authorized_roots,
    )?;
    if !resource_id.is_empty() && !valid_resource_id(resource_id) {
        return Err("resource registration id is not opaque".into());
    }
    if let Some(existing) =
        registry.active_for_path(account_id, workspace_id, &root_path, relative_path)
    {
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

#[cfg(unix)]
fn registry_destination(
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
        if (!binding.account_ids.is_empty() && !binding.account_ids.contains(account_id))
            || (!binding.workspace_ids.is_empty() && !binding.workspace_ids.contains(workspace_id))
        {
            return Err("provider root is unauthorized for this account/workspace".into());
        }
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
    Ok((root_id, root.to_string_lossy().into_owned()))
}

#[cfg(unix)]
fn resolve_registered_path(
    entry: &ResourceRegistration,
    host_root: &std::path::Path,
    authorized_roots: &[AuthorizedHistoryRoot],
) -> Result<std::path::PathBuf, String> {
    let root = if authorized_roots.is_empty() {
        ResourceRegistry::canonical_root(&entry.root_path, host_root)
            .map_err(|error| format!("registered root is unauthorized: {error}"))?
    } else {
        let binding = authorized_roots
            .iter()
            .find(|binding| binding.root_id == entry.root_id)
            .ok_or_else(|| "registered provider root is no longer authorized".to_owned())?;
        if (!binding.account_ids.is_empty() && !binding.account_ids.contains(&entry.account_id))
            || (!binding.workspace_ids.is_empty()
                && !binding.workspace_ids.contains(&entry.workspace_id))
        {
            return Err(
                "registered provider root is no longer authorized for this account/workspace"
                    .into(),
            );
        }
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
    Ok(resolved)
}

#[cfg(unix)]
fn resource_handle(registration: &ResourceRegistration) -> ResourceHandle {
    ResourceHandle {
        resource_id: registration.resource_id.clone(),
        account_id: registration.account_id.clone(),
        workspace_id: registration.workspace_id.clone(),
        generation: registration.generation,
    }
}

#[cfg(unix)]
#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    use bytes::Bytes;
    use std::env;
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;
    use std::sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    };
    use tokio::io::{AsyncWriteExt, BufReader};
    use tokio::net::UnixListener;

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
    let staging_root = resource_map
        .parent()
        .unwrap_or_else(|| std::path::Path::new("."))
        .join(".openclank-history-staging");
    std::fs::create_dir_all(&staging_root)?;
    if let Ok(entries) = std::fs::read_dir(&staging_root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| {
                    name.starts_with(".openclank-stage-") && name.ends_with(".part")
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
    let lock_path = format!("{socket}.lock");
    if Path::new(socket).exists() {
        let pid = std::fs::read_to_string(&lock_path)
            .ok()
            .and_then(|value| value.trim().parse::<u32>().ok());
        let alive = pid.is_some_and(|pid| {
            std::process::Command::new("kill")
                .args(["-0", &pid.to_string()])
                .status()
                .is_ok_and(|status| status.success())
        });
        if alive || !Path::new(&lock_path).exists() {
            return Err("refusing existing socket path".into());
        }
        std::fs::remove_file(socket)?;
    }
    if Path::new(&lock_path).exists() {
        let pid = std::fs::read_to_string(&lock_path)
            .ok()
            .and_then(|value| value.trim().parse::<u32>().ok());
        let alive = pid.is_some_and(|pid| {
            std::process::Command::new("kill")
                .args(["-0", &pid.to_string()])
                .status()
                .is_ok_and(|status| status.success())
        });
        if alive {
            return Err("history installation is already locked".into());
        }
        std::fs::remove_file(&lock_path)?;
    }
    let mut lock = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&lock_path)
        .map_err(|_| "history installation is already locked")?;
    let _resource_guard = SocketGuard {
        socket: socket.clone(),
        lock: lock_path.clone(),
    };
    use std::io::Write;
    if std::env::var("OPENCLANK_HISTORY_FAIL_STARTUP")
        .ok()
        .as_deref()
        == Some("pid")
    {
        return Err("injected pid startup failure".into());
    }
    writeln!(lock, "{}", std::process::id())?;
    if std::env::var("OPENCLANK_HISTORY_FAIL_STARTUP")
        .ok()
        .as_deref()
        == Some("bind")
    {
        return Err("injected bind startup failure".into());
    }
    let listener = UnixListener::bind(socket)?;
    if std::env::var("OPENCLANK_HISTORY_FAIL_STARTUP")
        .ok()
        .as_deref()
        == Some("chmod")
    {
        return Err("injected chmod startup failure".into());
    }
    std::fs::set_permissions(socket, std::fs::Permissions::from_mode(0o600))?;
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
    while !stopping.load(Ordering::Acquire) {
        let (stream, _) =
            match tokio::time::timeout(std::time::Duration::from_millis(100), listener.accept())
                .await
            {
                Ok(result) => result?,
                Err(_) => continue,
            };
        let coordinator = coordinator.clone();
        let stopping = stopping.clone();
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
        tokio::spawn(async move {
            let (read, mut write) = stream.into_split();
            let mut reader = BufReader::new(read);
            loop {
                let frame = match read_frame(&mut reader).await {
                    Ok(Some(frame)) => frame,
                    _ => break,
                };
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
                // StageBegin/Chunk/Finish/Abort arms below are retained as a wire-compatibility
                // fallback, but every valid staging request is consumed above and continues
                // before entering this coordinator-locked legacy match.
                let response = {
                    let coordinator = coordinator.lock().await;
                    match serde_json::from_slice::<ServiceRequest>(&frame) {
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
                                match begin(coordinator.catalog(), envelope.request.clone()) {
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
                        Ok(ServiceRequest::StageBegin {
                            envelope,
                            upload_id,
                            content_length,
                            fingerprint,
                        }) => match auth_bindings
                            .validate_control_cap(&envelope, "capture")
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
                                                        use std::os::unix::fs::PermissionsExt;
                                                        let _ = file.set_permissions(
                                                            std::fs::Permissions::from_mode(0o600),
                                                        );
                                                    }
                                                    uploads.insert(
                                                        upload_id,
                                                        StagedUpload {
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
                            .validate_control_cap(&envelope, "capture")
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
                            .validate_control_cap(&envelope, "capture")
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
                            .validate_control_cap(&envelope, "capture")
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
                                        suggested,
                                        &account_id,
                                        &workspace_id,
                                        root_id.as_deref(),
                                        &root_path,
                                        &relative_path,
                                        &host_root,
                                        &authorized_roots,
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
                                    if entry.account_id != envelope.auth.account_id {
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
                        Ok(ServiceRequest::GetPolicy(envelope)) => {
                            match auth_bindings.validate_control_cap(&envelope, "settings-read") {
                                Ok(binding) => match coordinator.policy_set() {
                                    Ok(mut policy) => {
                                        if !binding.capabilities.contains("admin") {
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
                                    if binding.capabilities.contains("admin") {
                                        return Ok(policy);
                                    }
                                    if policy.global != current.global
                                        || policy.scopes.iter().any(|scope| {
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
                                    Ok(merged)
                                });
                                match update.and_then(|next| {
                                    coordinator.set_policy(expected_revision, next)
                                }) {
                                    Ok(policy) => ServiceResponse::Policy(policy),
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
                                            if !binding.capabilities.contains("admin") {
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
                                        ServiceResponse::Status {
                                            state,
                                            reason,
                                            history_paused,
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

#[cfg(not(unix))]
fn main() {
    eprintln!("openclank-history-service: Windows transport is unsupported in this slice");
    std::process::exit(2);
}
