//! Exact-version restore planning. Providers apply a prepared plan through their own mutation
//! owner; this module never writes around a provider or guesses a destination revision.

use crate::catalog::{CatalogResult, ResourceKey, VersionReceipt};
use crate::operations::{ActionRequest, HistoryCoordinator, begin};
use bytes::Bytes;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::Digest;
use std::collections::HashSet;
use std::io::Write;
use std::path::{Component, Path, PathBuf};

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
    #[serde(default)]
    pub verification: Option<RestoreVerification>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RestoreVerification {
    pub restore_id: String,
    pub resource_id: String,
    pub version_id: String,
    pub content_hash: String,
    pub restored_content_hash: Option<String>,
    pub status: String,
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
    #[serde(default)]
    pub resource_type: Option<String>,
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
    #[serde(default)]
    pub verification: Option<RestoreVerification>,
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
        if std::fs::symlink_metadata(&self.path).is_err() {
            return Ok(None);
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            return Ok(Some(std::fs::symlink_metadata(&self.path)?.permissions().mode()));
        }
        #[cfg(not(unix))]
        { Ok(None) }
    }

    fn read_current(&self) -> CatalogResult<(Vec<u8>, Option<u32>)> {
        let metadata = match std::fs::symlink_metadata(&self.path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok((Vec::new(), None));
            }
            Err(error) => return Err(error.into()),
        };
        if metadata.file_type().is_symlink() {
            return Ok((std::fs::read_link(&self.path)?.to_string_lossy().as_bytes().to_vec(), self.mode()?));
        }
        if metadata.is_dir() {
            return Ok((directory_manifest(&self.path)?, self.mode()?));
        }
        Ok((std::fs::read(&self.path)?, self.mode()?))
    }

    fn persist_receipt(&self, restore_id: &str, digest: &str) -> CatalogResult<()> {
        let receipt_parent = self.receipt_path.parent().ok_or("receipt has no parent")?;
        std::fs::create_dir_all(receipt_parent)?;
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let receipt_temp = receipt_parent.join(format!(
            ".{}.tmp-{}-{nonce}",
            self.receipt_path.file_name().unwrap().to_string_lossy(),
            std::process::id()
        ));
        {
            use std::io::Write;
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&receipt_temp)?;
            let destination_key = blake3::hash(self.path.to_string_lossy().as_bytes()).to_hex();
            file.write_all(format!("v1\n{destination_key}\n{restore_id}\n{digest}").as_bytes())?;
            file.sync_all()?;
        }
        crate::platform::publish(&receipt_temp, &self.receipt_path)?;
        crate::platform::finish_publication(&self.receipt_path)?;
        Ok(())
    }
}

impl RestoreProvider for FilesystemRestoreProvider {
    fn current_fingerprint(&self) -> CatalogResult<Option<String>> {
        if std::fs::symlink_metadata(&self.path).is_err() {
            return Ok(None);
        }
        let (bytes, mode) = self.read_current()?;
        Ok(Some(Self::fingerprint_for(&bytes, mode)))
    }

    fn capture_current(&mut self) -> CatalogResult<CurrentStateReceipt> {
        if std::fs::symlink_metadata(&self.path).is_err() {
            return Ok(CurrentStateReceipt {
                version_id: "host:missing".into(),
                fingerprint: "missing".into(),
                content: Vec::new(),
                durable: true,
                host_metadata: HostMetadata {
                    native_locator: Some(self.path.to_string_lossy().into_owned()),
                    resource_type: Some("Missing".into()),
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
                modified_unix_millis: std::fs::symlink_metadata(&self.path)?.modified().ok().and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok()).map(|value| value.as_millis() as u64),
                resource_type: Some({
                    let metadata = std::fs::symlink_metadata(&self.path)?;
                    if metadata.file_type().is_symlink() {
                        "Symlink".into()
                    } else if metadata.is_dir() {
                        "Directory".into()
                    } else {
                        "File".into()
                    }
                }),
                symlink_target: std::fs::read_link(&self.path).ok().map(|target| target.to_string_lossy().into_owned()),
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
        if let Some(target) = host_metadata.and_then(|metadata| metadata.symlink_target.as_deref()) {
            {
                if std::fs::symlink_metadata(&self.path).is_ok() {
                    std::fs::remove_file(&self.path)?;
                }
                crate::platform::symlink(target, &self.path)?;
                crate::platform::finish_publication(&self.path)?;
                self.persist_receipt(restore_id, &digest)?;
                return Ok(());
            }
        }
    if host_metadata.and_then(|metadata| metadata.resource_type.as_deref()) == Some("Directory")
            || is_directory_manifest(content)
        {
            apply_directory_manifest(&self.path, content)
                .map_err(|error| format!("directory manifest apply failed: {error}"))?;
            if let Some(mode) = mode {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    std::fs::set_permissions(&self.path, std::fs::Permissions::from_mode(mode))?;
                }
            }
            if let Some(millis) = host_metadata.and_then(|metadata| metadata.modified_unix_millis) {
                set_modified_millis(&self.path, millis)?;
            }
            crate::platform::finish_publication(&self.path)?;
            self.persist_receipt(restore_id, &digest)?;
            return Ok(());
        }
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
        if let Some(millis) = host_metadata.and_then(|metadata| metadata.modified_unix_millis) {
            set_modified_millis(&temporary, millis)?;
        }
        crate::platform::publish(&temporary, &self.path)?;
        crate::platform::finish_publication(&self.path)?;
        self.persist_receipt(restore_id, &digest)?;
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

fn directory_manifest(path: &Path) -> CatalogResult<Vec<u8>> {
    fn walk(root: &Path, current: &Path, entries: &mut Vec<Value>) -> CatalogResult<()> {
        let mut children = std::fs::read_dir(current)?.collect::<Result<Vec<_>, _>>()?;
        children.sort_by_key(|entry| entry.file_name());
        for child in children {
            let item = child.path();
            let relative = item
                .strip_prefix(root)
                .map_err(|_| "directory manifest escaped its root")?
                .to_string_lossy()
                .replace(std::path::MAIN_SEPARATOR, "/");
            let metadata = std::fs::symlink_metadata(&item)?;
            let mut record = serde_json::json!({
                "path": relative,
                "mode": host_mode(&metadata),
                "mtime_millis": metadata.modified().ok().and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok()).map(|value| value.as_millis()),
            });
            if metadata.file_type().is_symlink() {
                record["type"] = Value::String("symlink".into());
                record["target"] = Value::String(std::fs::read_link(&item)?.to_string_lossy().into_owned());
            } else if metadata.is_dir() {
                record["type"] = Value::String("directory".into());
                walk(root, &item, entries)?;
            } else {
                let bytes = std::fs::read(&item)?;
                record["type"] = Value::String("file".into());
                record["size"] = Value::from(bytes.len() as u64);
                record["sha256"] = Value::String(format!("sha256:{:x}", sha2::Sha256::digest(&bytes)));
                record["content"] = Value::String(STANDARD.encode(bytes));
            }
            entries.push(record);
        }
        Ok(())
    }
    let mut entries = Vec::new();
    walk(path, path, &mut entries)?;
    Ok(serde_json::to_vec(&serde_json::json!({"version": 1, "root_type": "directory", "entries": entries}))?)
}

fn is_directory_manifest(content: &[u8]) -> bool {
    serde_json::from_slice::<Value>(content)
        .ok()
        .and_then(|value| value.get("root_type").and_then(Value::as_str).map(|kind| kind == "directory"))
        .unwrap_or(false)
}

struct ValidatedRestoreEntry {
    value: Value,
    relative: PathBuf,
    key: String,
    kind: String,
}

fn validate_restore_entries(entries: &[Value]) -> CatalogResult<Vec<ValidatedRestoreEntry>> {
    let mut validated = Vec::with_capacity(entries.len());
    let mut paths = HashSet::new();
    let mut symlink_paths = HashSet::new();
    for entry in entries {
        let relative = entry.get("path").and_then(Value::as_str).ok_or("directory entry has no path")?;
        let relative_path = Path::new(relative);
        let mut components = Vec::new();
        for component in relative_path.components() {
            match component {
                Component::Normal(value) => components.push(value.to_string_lossy().into_owned()),
                Component::CurDir | Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                    return Err("directory restore entry escapes destination".into());
                }
            }
        }
        if components.is_empty() {
            return Err("directory restore entry has an empty path".into());
        }
        let key = components.join("/");
        if !paths.insert(key.clone()) {
            return Err("directory restore manifest contains duplicate paths".into());
        }
        let kind = entry.get("type").and_then(Value::as_str).ok_or("directory entry has no type")?.to_owned();
        if !matches!(kind.as_str(), "directory" | "file" | "symlink") {
            return Err("directory restore entry has unsupported type".into());
        }
        if kind == "symlink" {
            symlink_paths.insert(key.clone());
        }
        validated.push(ValidatedRestoreEntry {
            value: entry.clone(),
            relative: relative_path.to_owned(),
            key,
            kind,
        });
    }
    for entry in &validated {
        let mut ancestor = Vec::new();
        let component_count = entry.relative.components().count();
        for component in entry.relative.components().take(component_count.saturating_sub(1)) {
            if let Component::Normal(value) = component {
                ancestor.push(value.to_string_lossy());
                if symlink_paths.contains(&ancestor.join("/")) {
                    return Err(format!("directory restore entry {} has a symlink ancestor", entry.key).into());
                }
            }
        }
    }
    Ok(validated)
}

fn ensure_safe_restore_parents(root: &Path, relative: &Path) -> CatalogResult<()> {
    let metadata = std::fs::symlink_metadata(root)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("directory restore root is not a real directory".into());
    }
    let mut current = root.to_owned();
    let component_count = relative.components().count();
    for component in relative.components().take(component_count.saturating_sub(1)) {
        let Component::Normal(value) = component else {
            return Err("directory restore parent is not a normal path".into());
        };
        current.push(value);
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err("directory restore parent is a symlink".into());
            }
            Ok(metadata) if !metadata.is_dir() => {
                return Err("directory restore parent is not a directory".into());
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                std::fs::create_dir(&current)?;
                let created = std::fs::symlink_metadata(&current)?;
                if created.file_type().is_symlink() || !created.is_dir() {
                    return Err("directory restore parent changed to a symlink".into());
                }
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn apply_directory_manifest(path: &Path, content: &[u8]) -> CatalogResult<()> {
    let manifest: Value = serde_json::from_slice(content)?;
    if manifest.get("version").and_then(Value::as_u64) != Some(1)
        || manifest.get("root_type").and_then(Value::as_str) != Some("directory")
    {
        return Err("directory restore manifest version or root type is invalid".into());
    }
    let entries = manifest
        .get("entries")
        .and_then(Value::as_array)
        .ok_or("directory restore manifest has no entries")?;
    let validated = validate_restore_entries(entries)?;
    if std::fs::symlink_metadata(path).is_ok() {
        let metadata = std::fs::symlink_metadata(path)?;
        if metadata.is_dir() && !metadata.file_type().is_symlink() {
            std::fs::remove_dir_all(path)?;
        } else {
            std::fs::remove_file(path)?;
        }
    }
    std::fs::create_dir(path)?;
    let mut ordered = validated;
    ordered.sort_by_key(|entry| {
        let kind_order = match entry.kind.as_str() {
            "directory" => 0_u8,
            "file" => 1,
            "symlink" => 2,
            _ => 3,
        };
        (kind_order, entry.key.clone())
    });
    for entry in &ordered {
        ensure_safe_restore_parents(path, &entry.relative)?;
        let target = path.join(&entry.relative);
        let value = &entry.value;
        match entry.kind.as_str() {
            "directory" => {
                match std::fs::symlink_metadata(&target) {
                    Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
                        return Err("directory restore target is not a real directory".into());
                    }
                    Ok(_) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => std::fs::create_dir(&target)?,
                    Err(error) => return Err(error.into()),
                }
            }
            "symlink" => {
                let link_target = value.get("target").and_then(Value::as_str).ok_or("symlink entry has no target")?;
                crate::platform::symlink(link_target, &target)?;
            }
            "file" => {
                let encoded = value.get("content").and_then(Value::as_str).ok_or("file entry has no retrievable content")?;
                let bytes = STANDARD.decode(encoded).map_err(|_| "file entry content is invalid base64")?;
                let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(&target)?;
                file.write_all(&bytes)?;
                file.sync_all()?;
            }
            _ => return Err("directory restore entry has unsupported type".into()),
        }
        if entry.kind != "symlink" {
            if let Some(mode) = value.get("mode").and_then(Value::as_u64) {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&target, std::fs::Permissions::from_mode(mode as u32))?;
            }
            }
        }
    }
    for entry in &ordered {
        ensure_safe_restore_parents(path, &entry.relative)?;
        let target = path.join(&entry.relative);
        if let Some(millis) = entry.value.get("mtime_millis").and_then(Value::as_u64) {
            set_modified_millis(&target, millis)?;
        }
    }
    Ok(())
}

fn set_modified_millis(path: &Path, millis: u64) -> CatalogResult<()> {
    #[cfg(unix)]
    {
        use std::ffi::CString;
        use std::os::unix::ffi::OsStrExt;
        let path = CString::new(path.as_os_str().as_bytes()).map_err(|_| "mtime path contains NUL")?;
        let time = libc::timespec {
            tv_sec: (millis / 1_000) as libc::time_t,
            tv_nsec: ((millis % 1_000) * 1_000_000) as libc::c_long,
        };
        let times = [time, time];
        let result = unsafe { libc::utimensat(libc::AT_FDCWD, path.as_ptr(), times.as_ptr(), libc::AT_SYMLINK_NOFOLLOW) };
        if result != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // OPEN_REPARSE_POINT changes the link itself rather than its target.
        let file = std::fs::OpenOptions::new().write(true).custom_flags(0x02200000).open(path)?;
        let modified = std::time::UNIX_EPOCH + std::time::Duration::from_millis(millis);
        file.set_times(std::fs::FileTimes::new().set_modified(modified))?;
    }
    Ok(())
}

fn host_mode(metadata: &std::fs::Metadata) -> Option<u32> {
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
            && !action
                .before_resources
                .iter()
                .filter_map(|resource| resource.before.as_ref())
                .chain(action.after_resources.iter().map(|resource| &resource.after))
                .any(|receipt| receipt.version_id == request.source_version_id && receipt == source)
    {
        return Err(RestoreOutcome::Unauthorized);
    }
    if let Some(journal) = coordinator.catalog().get_restore::<RestoreJournal>(&request.restore_id).map_err(|_| RestoreOutcome::Corrupt)? {
        if journal.source_action_id != request.source_action_id || journal.source_version_id != request.source_version_id
            || journal.destination != request.destination || journal.expected_destination_fingerprint != request.expected_destination_fingerprint {
            return Err(RestoreOutcome::Conflict);
        }
        let content = if journal.state == RestoreState::Complete { None } else {
            coordinator.read_version(&request.source_action_id, source).await.map_err(|_| RestoreOutcome::Corrupt)?.map(|bytes| bytes.to_vec())
        };
        return Ok(RestorePlan {
            source_action_id: request.source_action_id.clone(), source_version_id: request.source_version_id.clone(),
            destination: request.destination.clone(), expected_destination_fingerprint: request.expected_destination_fingerprint.clone(),
            observed_destination_fingerprint: observed_destination_fingerprint.map(str::to_owned),
            content, source_host_metadata: None, requires_current_capture: request.require_current_capture,
        });
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
    // Keep the CAS precondition typed at the authorization boundary.  The
    // lower planning helper still validates it defensively, but this path must
    // never classify a normal revision conflict by parsing an error string.
    if let (Some(expected), Some(observed)) = (
        request.expected_destination_fingerprint.as_deref(),
        observed_destination_fingerprint,
    ) {
        if expected != observed {
            return Err(RestoreOutcome::Conflict);
        }
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
    // Expiry was checked through the typed catalog state above. Any later
    // source-read failure is corrupt/unavailable data; do not classify a
    // restore outcome by parsing an implementation error string.
    .map_err(|_| RestoreOutcome::Corrupt)?;
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
        verification: None,
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
            verification: None,
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
            verification: None,
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

fn verify_restore<P: RestoreProvider + ?Sized>(journal: &mut RestoreJournal, content: &[u8], provider: &mut P) {
    let content_hash = format!("sha256:{:x}", sha2::Sha256::digest(content));
    let restored_content_hash = provider.capture_current().ok()
        .filter(|current| current.host_metadata.resource_type.as_deref() != Some("Missing"))
        .map(|current| format!("sha256:{:x}", sha2::Sha256::digest(&current.content)));
    let status = if restored_content_hash.as_deref() == Some(content_hash.as_str()) { "Verified" }
        else if restored_content_hash.is_some() { "Mismatch" } else { "Pending" };
    journal.verification = Some(RestoreVerification {
        restore_id: journal.restore_id.clone(), resource_id: journal.destination.resource_id.clone(),
        version_id: journal.source_version_id.clone(), content_hash, restored_content_hash,
        status: status.into(),
    });
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
        // A retry may verify a committed write, but must never invoke apply again.
        if journal.verification.as_ref().is_some_and(|proof| proof.status == "Pending") {
            let leases = journal.lease_ids.clone();
            coordinator.catalog().acquire_leases(&request.restore_id, &leases).map_err(|_| RestoreOutcome::Conflict)?;
            if let Ok(current) = provider.capture_current()
                && current.host_metadata.resource_type.as_deref() != Some("Missing") {
                let actual = format!("sha256:{:x}", sha2::Sha256::digest(&current.content));
                if let Some(proof) = journal.verification.as_mut() {
                    proof.status = if actual == proof.content_hash { "Verified".into() } else { "Mismatch".into() };
                    proof.restored_content_hash = Some(actual);
                }
                let stored = coordinator.catalog().put_restore(&request.restore_id, &journal);
                let _ = coordinator.catalog().release_leases(&request.restore_id, &leases);
                stored.map_err(|_| RestoreOutcome::Corrupt)?;
            } else {
                let _ = coordinator.catalog().release_leases(&request.restore_id, &leases);
            }
        }
        let outcome = journal.outcome.clone().unwrap_or(RestoreOutcome::Complete);
        return Ok(RestoreReceipt {
            verification: journal.verification.clone(),
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
            verify_restore(&mut journal, content, provider);
            journal.state = RestoreState::Complete;
            journal.outcome = Some(RestoreOutcome::Complete);
            coordinator
                .catalog()
                .put_restore(&request.restore_id, &journal)
                .map_err(|_| RestoreOutcome::Corrupt)?;
            return Ok(RestoreReceipt {
                verification: journal.verification.clone(),
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
        verify_restore(&mut journal, content, provider);
        journal.state = RestoreState::Complete;
        journal.outcome = Some(RestoreOutcome::Complete);
        coordinator
            .catalog()
            .put_restore(&request.restore_id, &journal)
            .map_err(|_| RestoreOutcome::Corrupt)?;
        Ok(RestoreReceipt {
            verification: journal.verification.clone(),
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
        verification: None,
        source_action_id: plan.source_action_id.clone(),
        source_version_id: plan.source_version_id.clone(),
        outcome: RestoreOutcome::Complete,
        resources: vec![RestoreResourceOutcome {
            resource_id: plan.destination.resource_id.clone(),
            outcome: RestoreOutcome::Complete,
        }],
    })
}
