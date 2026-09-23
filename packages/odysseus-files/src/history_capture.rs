//! Authenticated mutation-owner adapter for the local history service.
//!
//! This module intentionally lives beside `FileEngine`: HTTP/UI callers cannot
//! bypass the preimage boundary. Capture failures are retained as per-action
//! status and never turn an otherwise successful filesystem mutation into a
//! false protected-history receipt.

use crate::{
    CaptureAfter, CaptureStatus, CaptureTarget, MutationCaptureHook, MutationCaptureTicket,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

const INLINE_CONTENT_BYTES: usize = 640 * 1024;
const STAGE_CHUNK_BYTES: usize = 512 * 1024;

#[cfg(unix)]
use std::io::{BufRead, BufReader, Write};
#[cfg(unix)]
use std::os::unix::net::UnixStream;

#[derive(Clone)]
pub struct HistoryServiceHook {
    socket_path: PathBuf,
    actor_id: String,
    account_id: String,
    workspace_id: String,
    authorized_roots: Vec<AuthorizedHistoryRoot>,
    token: String,
    actor_kind: String,
    statuses: Arc<Mutex<HashMap<String, CaptureStatus>>>,
    sequence: Arc<Mutex<u64>>,
    last_action: Arc<Mutex<Option<String>>>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct AuthorizedHistoryRoot {
    root_id: String,
    canonical_path: PathBuf,
    #[serde(default = "default_root_kind")]
    kind: String,
}

fn default_root_kind() -> String {
    "recursive_directory".into()
}

impl AuthorizedHistoryRoot {
    fn contains(&self, path: &Path) -> bool {
        if self.kind == "exact_file" {
            path == self.canonical_path
        } else {
            path.starts_with(&self.canonical_path)
        }
    }
}

impl HistoryServiceHook {
    pub fn new(
        socket_path: impl Into<PathBuf>,
        actor_id: impl Into<String>,
        account_id: impl Into<String>,
        workspace_id: impl Into<String>,
        workspace_root: impl Into<PathBuf>,
        token: impl Into<String>,
    ) -> Result<Arc<Self>, String> {
        Self::new_with_actor_kind(
            socket_path,
            actor_id,
            account_id,
            workspace_id,
            workspace_root,
            token,
            "agent",
        )
    }

    pub fn new_with_actor_kind(
        socket_path: impl Into<PathBuf>,
        actor_id: impl Into<String>,
        account_id: impl Into<String>,
        workspace_id: impl Into<String>,
        workspace_root: impl Into<PathBuf>,
        token: impl Into<String>,
        actor_kind: impl Into<String>,
    ) -> Result<Arc<Self>, String> {
        let actor_id = actor_id.into();
        let account_id = account_id.into();
        let workspace_id = workspace_id.into();
        let workspace_root =
            fs::canonicalize(workspace_root.into()).map_err(|error| error.to_string())?;
        if actor_id.trim().is_empty()
            || account_id.trim().is_empty()
            || workspace_id.trim().is_empty()
        {
            return Err("history identity is incomplete".into());
        }
        let authorized_roots = match std::env::var("OPENCLANK_HISTORY_ROOT_BINDINGS") {
            Ok(raw) if !raw.trim().is_empty() => {
                let parsed = serde_json::from_str::<Vec<AuthorizedHistoryRoot>>(&raw)
                    .map_err(|error| format!("invalid history root bindings: {error}"))?;
                if parsed.is_empty() {
                    return Err("history root bindings are empty".into());
                }
                parsed
                    .into_iter()
                    .map(|mut root| {
                        root.canonical_path = fs::canonicalize(&root.canonical_path)
                            .map_err(|error| format!("history root is unavailable: {error}"))?;
                        if root.root_id.trim().is_empty() {
                            return Err("history root identity is empty".into());
                        }
                        if !matches!(root.kind.as_str(), "exact_file" | "recursive_directory") {
                            return Err("history root kind is invalid".into());
                        }
                        if root.kind == "exact_file" && !root.canonical_path.is_file() {
                            return Err("exact-file history root is not a file".into());
                        }
                        if root.kind == "recursive_directory" && !root.canonical_path.is_dir() {
                            return Err("recursive history root is not a directory".into());
                        }
                        Ok(root)
                    })
                    .collect::<Result<Vec<_>, String>>()?
            }
            _ => vec![AuthorizedHistoryRoot {
                root_id: "workspace-root".into(),
                canonical_path: workspace_root.clone(),
                kind: "recursive_directory".into(),
            }],
        };
        Ok(Arc::new(Self {
            socket_path: socket_path.into(),
            actor_id,
            account_id,
            workspace_id,
            authorized_roots,
            token: token.into(),
            actor_kind: actor_kind.into(),
            statuses: Arc::new(Mutex::new(HashMap::new())),
            sequence: Arc::new(Mutex::new(0)),
            last_action: Arc::new(Mutex::new(None)),
        }))
    }

    fn next_action_id(&self) -> String {
        let mut sequence = self.sequence.lock().expect("history sequence lock");
        *sequence = sequence.saturating_add(1);
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
        let action_id = format!("odysseus-{millis:x}-{:x}", *sequence);
        if let Ok(mut last) = self.last_action.lock() {
            *last = Some(action_id.clone());
        }
        action_id
    }

    fn set_status(&self, action_id: &str, status: &str, phase: &str, error: Option<String>) {
        if let Ok(mut statuses) = self.statuses.lock() {
            statuses.insert(
                action_id.to_owned(),
                CaptureStatus {
                    action_id: action_id.to_owned(),
                    status: status.to_owned(),
                    phase: phase.to_owned(),
                    error,
                },
            );
        }
    }

    fn allowed(&self, path: &Path) -> bool {
        let path = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
        if self
            .authorized_roots
            .iter()
            .all(|root| !root.contains(&path))
        {
            return false;
        }
        path.components().all(|component| {
            let value = component.as_os_str().to_string_lossy().to_ascii_lowercase();
            !matches!(
                value.as_str(),
                ".env" | ".secrets" | "auth" | "credentials" | "tokens" | "settings"
            )
        })
    }

    fn root_for(&self, path: &Path) -> Result<(&AuthorizedHistoryRoot, PathBuf), String> {
        let canonical = self.canonical_target(path)?;
        self.authorized_roots
            .iter()
            .filter(|root| root.contains(&canonical))
            .max_by_key(|root| root.canonical_path.components().count())
            .map(|root| (root, canonical))
            .ok_or_else(|| "resource is outside the authorized Files roots".into())
    }

    fn request(&self, payload: Value) -> Result<Value, String> {
        #[cfg(unix)]
        {
            let mut stream =
                UnixStream::connect(&self.socket_path).map_err(|error| error.to_string())?;
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(10)))
                .map_err(|error| error.to_string())?;
            let mut frame = serde_json::to_vec(&payload).map_err(|error| error.to_string())?;
            frame.push(b'\n');
            if frame.len() > 1024 * 1024 {
                return Err("history frame exceeds the 1 MiB service limit".into());
            }
            stream
                .write_all(&frame)
                .map_err(|error| error.to_string())?;
            // The history service keeps accepted connections alive for the
            // next request. Read one bounded newline-delimited response;
            // waiting for EOF would turn every real request into a timeout.
            let mut reader = BufReader::new(stream);
            let mut response = Vec::new();
            let read = reader
                .read_until(b'\n', &mut response)
                .map_err(|error| error.to_string())?;
            if read == 0 || response.last() != Some(&b'\n') {
                return Err("history service returned an unterminated response frame".into());
            }
            response.pop();
            if response.len() > 1024 * 1024 {
                return Err("history response exceeds the 1 MiB service limit".into());
            }
            let value: Value =
                serde_json::from_slice(&response).map_err(|error| error.to_string())?;
            if value.get("Error").is_some() || value.get("error").is_some() {
                return Err(value.to_string());
            }
            Ok(value)
        }
        #[cfg(not(unix))]
        {
            let _ = payload;
            Err("history IPC is unsupported on this platform".into())
        }
    }

    fn auth(&self) -> Value {
        json!({"actor_id": self.actor_id, "account_id": self.account_id, "token": self.token})
    }

    fn fingerprint(bytes: Option<&[u8]>) -> String {
        match bytes {
            None => "missing".into(),
            Some(bytes) => format!(
                "sha256:{}:{}",
                encode_hex(&Sha256::digest(bytes)),
                bytes.len()
            ),
        }
    }

    fn stage_content(
        &self,
        action_id: &str,
        content: &[u8],
        fingerprint: &str,
    ) -> Result<String, String> {
        if action_id.trim().is_empty() {
            return Err("history staged capture requires an action id".into());
        }
        let mut random = [0_u8; 24];
        getrandom::fill(&mut random).map_err(|error| error.to_string())?;
        let upload_id = format!("odysseus-stage-{}", encode_hex(&random));
        let result = (|| {
            self.request(json!({
                "StageBegin": {
                    "envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id},
                    "upload_id": upload_id,
                    "content_length": content.len(),
                    "fingerprint": fingerprint,
                }
            }))?;
            for (offset, chunk) in content.chunks(STAGE_CHUNK_BYTES).enumerate() {
                let offset = offset * STAGE_CHUNK_BYTES;
                self.request(json!({
                    "StageChunk": {
                        "envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id},
                        "upload_id": upload_id,
                        "offset": offset,
                        "content": encode_base64(chunk),
                    }
                }))?;
            }
            self.request(json!({
                "StageFinish": {
                    "envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id},
                    "upload_id": upload_id,
                }
            }))?;
            Ok(upload_id.clone())
        })();
        if result.is_err() {
            let _ = self.request(json!({
                "StageAbort": {
                    "envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id},
                    "upload_id": upload_id,
                }
            }));
        }
        result
    }

    fn canonical_target(&self, path: &Path) -> Result<PathBuf, String> {
        if let Ok(existing) = fs::canonicalize(path) {
            return Ok(existing);
        }
        let parent = path
            .parent()
            .ok_or_else(|| "resource has no parent".to_owned())?;
        let parent = fs::canonicalize(parent).map_err(|error| error.to_string())?;
        let name = path
            .file_name()
            .ok_or_else(|| "resource has no filename".to_owned())?;
        Ok(parent.join(name))
    }

    /// Ask the history worker to own the path-to-id binding.  The provider
    /// sends only its already-authorized root and a relative path; the worker
    /// validates both and returns an opaque handle without returning the
    /// private absolute path map.
    fn register_resource(&self, path: &Path) -> Result<(String, bool), String> {
        let (root, canonical) = self.root_for(path)?;
        let relative_path = canonical
            .strip_prefix(&root.canonical_path)
            .map_err(|_| "resource is outside the authorized Files root".to_owned())?
            .to_string_lossy()
            .replace('\\', "/");
        let relative_path = if relative_path.is_empty() {
            ".".to_owned()
        } else {
            relative_path
        };
        let response = self.request(json!({
            "RegisterResource": {
                "envelope": {
                    "protocol_version": 1,
                    "auth": self.auth(),
                    "action_id": "resource-registry"
                },
                "account_id": self.account_id,
                "workspace_id": self.workspace_id,
                "root_id": root.root_id,
                "root_path": root.canonical_path,
                "relative_path": relative_path,
                "resource_id": null
            }
        }))?;
        let resource = response
            .get("Resource")
            .and_then(|value| value.get("handle"))
            .ok_or_else(|| "history service returned no resource handle".to_owned())?;
        let resource_id = resource
            .get("resource_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| "history service returned an invalid resource handle".to_owned())?
            .to_owned();
        let created = response
            .get("Resource")
            .and_then(|value| value.get("created"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        Ok((resource_id, created))
    }

    fn update_resource(&self, resource_id: &str, path: &Path) -> Result<(), String> {
        let (root, canonical) = self.root_for(path)?;
        let relative_path = canonical
            .strip_prefix(&root.canonical_path)
            .map_err(|_| "resource is outside the authorized Files root".to_owned())?
            .to_string_lossy()
            .replace('\\', "/");
        let relative_path = if relative_path.is_empty() {
            ".".to_owned()
        } else {
            relative_path
        };
        self.request(json!({
            "UpdateResource": {
                "envelope": {
                    "protocol_version": 1,
                    "auth": self.auth(),
                    "action_id": "resource-registry"
                },
                "resource_id": resource_id,
                "account_id": self.account_id,
                "workspace_id": self.workspace_id,
                "root_id": root.root_id,
                "root_path": root.canonical_path,
                "relative_path": relative_path
            }
        }))?;
        Ok(())
    }

    fn move_resource(
        &self,
        resource_id: &str,
        replaced_resource_id: Option<&str>,
        path: &Path,
    ) -> Result<(), String> {
        let (root, canonical) = self.root_for(path)?;
        let relative_path = canonical
            .strip_prefix(&root.canonical_path)
            .map_err(|_| "resource is outside the authorized Files root".to_owned())?
            .to_string_lossy()
            .replace('\\', "/");
        let relative_path = if relative_path.is_empty() {
            ".".to_owned()
        } else {
            relative_path
        };
        self.request(json!({
            "MoveResource": {
                "envelope": {
                    "protocol_version": 1,
                    "auth": self.auth(),
                    "action_id": "resource-registry"
                },
                "resource_id": resource_id,
                "replaced_resource_id": replaced_resource_id,
                "account_id": self.account_id,
                "workspace_id": self.workspace_id,
                "root_id": root.root_id,
                "root_path": root.canonical_path,
                "relative_path": relative_path
            }
        }))?;
        Ok(())
    }

    fn revoke_resource(&self, resource_id: &str, reason: &str) -> Result<(), String> {
        self.request(json!({
            "RevokeResource": {
                "envelope": {
                    "protocol_version": 1,
                    "auth": self.auth(),
                    "action_id": "resource-registry"
                },
                "resource_id": resource_id,
                "reason": reason
            }
        }))?;
        Ok(())
    }
}

#[derive(Clone)]
enum RegistryCommit {
    Move {
        source_id: String,
        destination_id: String,
        destination: PathBuf,
    },
    Revoke {
        resource_id: String,
    },
}

impl MutationCaptureHook for HistoryServiceHook {
    fn prepare(
        &self,
        operation: &str,
        targets: &[CaptureTarget],
    ) -> Result<Box<dyn MutationCaptureTicket>, String> {
        let action_id = self.next_action_id();
        if targets.is_empty()
            || targets
                .iter()
                .any(|target| !self.allowed(&target.resource_id))
        {
            self.set_status(
                &action_id,
                "paused",
                "excluded",
                Some("target is outside the authorized workspace root".into()),
            );
            return Err("target is outside the authorized workspace root".into());
        }
        let first = &targets[0];
        let mut resource_ids = Vec::with_capacity(targets.len());
        let mut created_resource_ids: Vec<String> = Vec::new();
        for target in targets {
            let (resource_id, created) = match self.register_resource(&target.resource_id) {
                Ok(result) => result,
                Err(error) => {
                    self.set_status(&action_id, "paused", "registry", Some(error.clone()));
                    for resource_id in &created_resource_ids {
                        let _ = self.revoke_resource(resource_id, "resource registration failed");
                    }
                    return Err(error);
                }
            };
            if created {
                created_resource_ids.push(resource_id.clone());
            }
            resource_ids.push(resource_id);
        }
        let registry_commit = match operation {
            "move" | "rename" | "restore" if targets.len() > 1 => Some(RegistryCommit::Move {
                source_id: resource_ids[0].clone(),
                destination_id: resource_ids[1].clone(),
                destination: self.canonical_target(&targets[1].resource_id)?,
            }),
            "trash" => Some(RegistryCommit::Revoke {
                resource_id: resource_ids[0].clone(),
            }),
            _ => None,
        };
        let modified = targets
            .iter()
            .zip(resource_ids.iter())
            .map(|(_target, resource_id)| {
                json!({
                    "account_id": self.account_id,
                    "workspace_id": self.workspace_id,
                    "provider": "odysseus-files",
                    "resource_id": resource_id,
                })
            })
            .collect::<Vec<_>>();
        let request = json!({
            "schema_version": 1,
            "action_id": action_id,
            "actor_account_id": self.account_id,
            "resource_key": modified[0],
            "guard_resource_ids": [],
            "modified_resource_ids": modified,
            "operation": operation,
            "actor_id": self.actor_id,
            "actor_kind": self.actor_kind,
            "tool_id": "odysseus-files",
            "timestamp_millis": SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis(),
            "coverage": {"metadata": {"coverage_kind": if targets.len() > 1 { "Partial" } else { "KnownMutationHooks" }, "roots": self.authorized_roots.iter().map(|root| format!("root:{}", root.root_id)).collect::<Vec<_>>(), "exclusions": if targets.len() > 1 { vec!["non-primary targets use resource outcomes"] } else { Vec::<&str>::new() }}},
            "per_resource_outcomes": targets.iter().zip(resource_ids.iter()).map(|(target, resource_id)| json!({"resource_id": resource_id, "coverage": if target.resource_id == first.resource_id { "ExactBeforeOnly" } else { "ObservedAfterOnly" }, "before_fingerprint": Self::fingerprint(target.before.as_deref())})).collect::<Vec<_>>(),
        });
        let envelope = json!({
            "protocol_version": 1,
            "auth": self.auth(),
            "claimed_digest": "",
            "request": request,
        });
        let mut wire_entries = Vec::with_capacity(targets.len());
        let mut staged_uploads = Vec::new();
        let mut inline_bytes = 0usize;
        for (target, resource_id) in targets.iter().zip(resource_ids.iter()) {
            let fingerprint = Self::fingerprint(target.before.as_deref());
            let (resource_type, metadata) = match fs::symlink_metadata(&target.resource_id) {
                Ok(stat) if stat.file_type().is_symlink() => (
                    "Symlink",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": null}),
                ),
                Ok(stat) if stat.is_dir() => (
                    "Directory",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": null}),
                ),
                Ok(stat) => (
                    "File",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": null}),
                ),
                Err(_) => ("File", json!({"mode": null, "size": null, "modified_millis": null, "opaque": null})),
            };
            let mut entry = json!({
                "resource_key": {
                    "account_id": self.account_id,
                    "workspace_id": self.workspace_id,
                    "provider": "odysseus-files",
                    "resource_id": resource_id,
                },
                "old_locator": {"display_name": target.resource_id.file_name().map(|v| v.to_string_lossy()).unwrap_or_default(), "location_label": target.resource_id.to_string_lossy(), "opaque_ref": resource_id},
                "new_locator": {"display_name": target.resource_id.file_name().map(|v| v.to_string_lossy()).unwrap_or_default(), "location_label": target.resource_id.to_string_lossy(), "opaque_ref": resource_id},
                "expected_revision": {"Opaque": {"kind": "fingerprint", "value": fingerprint}},
                "existence": if target.before.is_some() { "Present" } else { "Absent" },
                "resource_type": resource_type,
                "metadata": metadata,
                "content": target.before.as_deref().map(encode_base64),
                "staged_upload_id": null,
                "fingerprint": fingerprint,
                "coverage": {"byte_len": target.before.as_ref().map_or(0, |bytes| bytes.len()), "metadata": {"exact_preimage": true}},
            });
            if resource_type == "Directory" {
                if let Some(content) = target.before.as_deref() {
                    let manifest_digest = format!("sha256:{:x}", Sha256::digest(content));
                    entry["metadata"]["opaque"] = json!({"manifest_digest": manifest_digest});
                    entry["coverage"]["content_digest"] = json!(manifest_digest);
                }
            }
            if let Some(content) = target.before.as_deref() {
                if content.len() > INLINE_CONTENT_BYTES || inline_bytes.saturating_add(content.len()) > STAGE_CHUNK_BYTES {
                    let upload_id = self.stage_content(&action_id, content, &fingerprint)?;
                    entry["content"] = Value::Null;
                    entry["staged_upload_id"] = Value::String(upload_id.clone());
                    staged_uploads.push(upload_id);
                } else {
                    inline_bytes = inline_bytes.saturating_add(content.len());
                }
            }
            wire_entries.push(entry);
        }
        let prepared = self.request(json!({"PrepareBatch": {"envelope": envelope, "batch_version": 1, "entries": wire_entries}}));
        if prepared.is_err() {
            for upload_id in &staged_uploads {
                let _ = self.request(json!({"StageAbort": {"envelope": {"protocol_version": 1, "auth": self.auth(), "action_id": action_id}, "upload_id": upload_id}}));
            }
        }
        if let Err(error) = prepared {
            for resource_id in &created_resource_ids {
                let _ = self.revoke_resource(resource_id, "history prepare failed");
            }
            return Err(error);
        }
        self.set_status(&action_id, "prepared", "before_durable", None);
        Ok(Box::new(ServiceTicket {
            hook: Arc::new(self.clone()),
            action_id,
            created_resource_ids,
            registry_commit,
            resource_ids,
        }))
    }

    fn status(&self, action_id: &str) -> Option<CaptureStatus> {
        self.statuses.lock().ok()?.get(action_id).cloned()
    }

    fn last_action_id(&self) -> Option<String> {
        self.last_action.lock().ok()?.clone()
    }
}

struct ServiceTicket {
    hook: Arc<HistoryServiceHook>,
    action_id: String,
    created_resource_ids: Vec<String>,
    registry_commit: Option<RegistryCommit>,
    resource_ids: Vec<String>,
}

impl MutationCaptureTicket for ServiceTicket {
    fn action_id(&self) -> &str {
        &self.action_id
    }

    fn complete(self: Box<Self>, after: Vec<CaptureAfter>) -> Result<(), String> {
        let auth = self.hook.auth();
        let control = json!({"protocol_version": 1, "auth": auth, "action_id": self.action_id});
        let first = after.first();
        let content = first.and_then(|target| target.after.as_deref());
        let live = json!({"action_id": self.action_id, "status": "Committed", "fingerprint": HistoryServiceHook::fingerprint(content)});
        // The physical mutation has already committed. Move the service-owned
        // identity first, then publish the live receipt. If the registry is
        // temporarily unavailable we still drive the action to a terminal
        // capture state below so no live action is stranded in Applying.
        let mut registry_error = None;
        if let Some(commit) = self.registry_commit.as_ref() {
            let result = match commit {
                RegistryCommit::Move {
                    source_id,
                    destination_id,
                    destination,
                } => self.hook.move_resource(
                    source_id,
                    (destination_id != source_id).then_some(destination_id.as_str()),
                    destination,
                ),
                RegistryCommit::Revoke { resource_id } => {
                    self.hook.revoke_resource(resource_id, "resource deleted")
                }
            };
            if let Err(error) = result {
                self.hook.set_status(
                    &self.action_id,
                    "pending",
                    "registry-reconcile",
                    Some(error.clone()),
                );
                registry_error = Some(error);
            }
        }
        if let Err(error) = self
            .hook
            .request(json!({"RecordLive": {"envelope": control, "receipt": live}}))
        {
            self.hook
                .set_status(&self.action_id, "failed", "live", Some(error.clone()));
            return Err(error);
        }
        let mut complete_entries = Vec::with_capacity(after.len());
        let mut staged_uploads = Vec::new();
        for (item, resource_id) in after.iter().zip(self.resource_ids.iter()) {
            let fingerprint = HistoryServiceHook::fingerprint(item.after.as_deref());
            let (resource_type, metadata) = match fs::symlink_metadata(resource_id) {
                Ok(stat) if stat.file_type().is_symlink() => (
                    "Symlink",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": {"target": fs::read_link(resource_id).ok().map(|target| target.to_string_lossy().into_owned())}}),
                ),
                Ok(stat) if stat.is_dir() => (
                    "Directory",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": {"manifest_digest": item.after.as_deref().map(|bytes| format!("sha256:{:x}", Sha256::digest(bytes)))}}),
                ),
                Ok(stat) => (
                    "File",
                    json!({"mode": mode(&stat), "size": stat.len(), "modified_millis": modified_millis(&stat), "opaque": null}),
                ),
                Err(_) => ("File", json!({"mode": null, "size": null, "modified_millis": null, "opaque": null})),
            };
            let after_digest = item
                .after
                .as_deref()
                .map(|bytes| format!("sha256:{:x}", Sha256::digest(bytes)));
            let mut entry = json!({
                "resource_key": {"account_id": self.hook.account_id, "workspace_id": self.hook.workspace_id, "provider": "odysseus-files", "resource_id": resource_id},
                "locator": {"display_name": Path::new(resource_id).file_name().map(|name| name.to_string_lossy()).unwrap_or_default(), "location_label": resource_id, "opaque_ref": resource_id},
                "existence": if item.after.is_some() { "Present" } else { "Absent" },
                "resource_type": resource_type,
                "metadata": metadata,
                "content": item.after.as_deref().map(encode_base64),
                "staged_upload_id": null,
                "fingerprint": fingerprint,
                "coverage": {"byte_len": item.after.as_ref().map_or(0, |bytes| bytes.len()), "content_digest": after_digest, "metadata": {"exact_after": true}},
                "outcome": {"resource_id": resource_id, "status": "Committed", "revision": {"Opaque": {"kind": "fingerprint", "value": fingerprint}}},
            });
            if let Some(content) = item.after.as_deref() {
                if content.len() > INLINE_CONTENT_BYTES {
                    let upload_id = self.hook.stage_content(&self.action_id, content, &fingerprint)?;
                    entry["content"] = Value::Null;
                    entry["staged_upload_id"] = Value::String(upload_id.clone());
                    staged_uploads.push(upload_id);
                }
            }
            complete_entries.push(entry);
        }
        let result = self.hook.request(json!({"CompleteBatch": {"envelope": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id}, "batch_version": 1, "entries": complete_entries}}));
        if result.is_err() {
            for upload_id in &staged_uploads {
                let _ = self.hook.request(json!({"StageAbort": {"envelope": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id}, "upload_id": upload_id}}));
            }
        }
        match result {
            Ok(_) if registry_error.is_some() => self.hook.set_status(
                &self.action_id,
                "failed",
                "registry-reconcile",
                registry_error.clone(),
            ),
            Ok(_) => self
                .hook
                .set_status(&self.action_id, "complete", "complete", None),
            Err(error) => self
                .hook
                .set_status(&self.action_id, "failed", "after", Some(error)),
        }
        self.hook
            .status(&self.action_id)
            .filter(|status| status.status == "complete")
            .map(|_| ())
            .ok_or_else(|| "history after capture failed".into())
    }

    fn abort(self: Box<Self>) {
        let _ = self.hook.request(json!({"Abort": {"envelope": {"protocol_version": 1, "auth": self.hook.auth(), "action_id": self.action_id}}}));
        for resource_id in &self.created_resource_ids {
            let _ = self
                .hook
                .revoke_resource(resource_id, "live mutation aborted");
        }
        self.hook
            .set_status(&self.action_id, "aborted", "aborted", None);
    }
}

fn encode_base64(bytes: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let first = chunk[0] as u32;
        let second = chunk.get(1).copied().unwrap_or(0) as u32;
        let third = chunk.get(2).copied().unwrap_or(0) as u32;
        output.push(TABLE[((first >> 2) & 63) as usize] as char);
        output.push(TABLE[((first << 4 | second >> 4) & 63) as usize] as char);
        output.push(if chunk.len() > 1 {
            TABLE[((second << 2 | third >> 6) & 63) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            TABLE[(third & 63) as usize] as char
        } else {
            '='
        });
    }
    output
}

fn mode(metadata: &std::fs::Metadata) -> Option<u32> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        Some(metadata.permissions().mode())
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
        None
    }
}

fn modified_millis(metadata: &std::fs::Metadata) -> Option<u64> {
    metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_millis() as u64)
}

/// Exact recursive directory payload shared by all mutation owners. The
/// payload is opaque to the history worker but content-bound by its digest.
pub(crate) fn directory_manifest(path: &Path) -> Result<Vec<u8>, String> {
    let root = fs::canonicalize(path).map_err(|error| error.to_string())?;
    let mut entries = Vec::new();
    let mut stack = vec![root.clone()];
    while let Some(current) = stack.pop() {
        let mut children = fs::read_dir(&current)
            .map_err(|error| error.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())?;
        children.sort_by_key(|entry| entry.file_name());
        for entry in children.into_iter().rev() {
            let path = entry.path();
            let relative = path
                .strip_prefix(&root)
                .map_err(|error| error.to_string())?
                .to_string_lossy()
                .replace('\\', "/");
            let metadata = fs::symlink_metadata(&path).map_err(|error| error.to_string())?;
            let mut record = json!({
                "path": relative,
                "mode": mode(&metadata),
                "mtime_millis": modified_millis(&metadata),
            });
            if metadata.file_type().is_symlink() {
                record["type"] = json!("symlink");
                record["target"] = json!(fs::read_link(&path).map_err(|error| error.to_string())?.to_string_lossy());
            } else if metadata.is_dir() {
                record["type"] = json!("directory");
                stack.push(path);
            } else {
                let bytes = fs::read(&path).map_err(|error| error.to_string())?;
                record["type"] = json!("file");
                record["size"] = json!(bytes.len());
                record["sha256"] = json!(format!("sha256:{}", encode_hex(&Sha256::digest(&bytes))));
            }
            entries.push(record);
        }
    }
    serde_json::to_vec(&json!({"version": 1, "root_type": "directory", "entries": entries}))
        .map_err(|error| error.to_string())
}

fn encode_hex(bytes: &[u8]) -> String {
    const TABLE: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(TABLE[(byte >> 4) as usize] as char);
        output.push(TABLE[(byte & 0x0f) as usize] as char);
    }
    output
}
