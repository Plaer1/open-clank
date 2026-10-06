//! Service-owned opaque resource registry.
//!
//! A history resource key is deliberately independent from the path a provider
//! currently uses to reach a file.  This registry is the small durable bridge
//! between those two identities.  Providers may ask the service to register or
//! move a resource, but they never write this file themselves.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub const RESOURCE_REGISTRY_VERSION: u32 = 1;

static RESOURCE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// The private mapping held by the history service.  `root_path` is retained
/// inside the service-owned file so a restore can verify the exact allowlisted
/// root before it ever resolves a destination.  It is never returned to an
/// unauthenticated caller.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceRegistration {
    pub resource_id: String,
    /// Persist lane restriction so withdrawing a root never reopens old history to wildcard credentials.
    #[serde(default)]
    pub actor_ids: std::collections::BTreeSet<String>,
    pub account_id: String,
    pub workspace_id: String,
    #[serde(default)]
    pub root_id: String,
    pub root_path: String,
    pub relative_path: String,
    #[serde(default = "default_active")]
    pub active: bool,
    #[serde(default)]
    pub generation: u64,
    #[serde(default)]
    pub updated_millis: u64,
}

fn default_active() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ResourceRegistry {
    pub version: u32,
    #[serde(default)]
    pub entries: BTreeMap<String, ResourceRegistration>,
}

impl Default for ResourceRegistry {
    fn default() -> Self {
        Self {
            version: RESOURCE_REGISTRY_VERSION,
            entries: BTreeMap::new(),
        }
    }
}

impl ResourceRegistry {
    pub fn load(path: &Path) -> io::Result<Self> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let bytes = fs::read(path)?;
        if bytes.is_empty() {
            return Ok(Self::default());
        }
        if let Ok(registry) = serde_json::from_slice::<Self>(&bytes) {
            if registry.version != RESOURCE_REGISTRY_VERSION {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "unsupported resource registry version",
                ));
            }
            // The map key is part of the integrity boundary.  Do not accept
            // a file whose value silently names another opaque resource.
            if registry
                .entries
                .iter()
                .any(|(key, entry)| key != &entry.resource_id)
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "resource registry key/value mismatch",
                ));
            }
            return Ok(registry);
        }

        // Upgrade the short-lived pre-service format once.  This reader is
        // intentionally strict and never writes the legacy representation.
        let legacy = serde_json::from_slice::<BTreeMap<String, String>>(&bytes).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidData, "invalid resource registry")
        })?;
        let mut upgraded = Self::default();
        for (resource_id, relative_path) in legacy {
            upgraded.entries.insert(
                resource_id.clone(),
                ResourceRegistration {
                    actor_ids: Default::default(),
                    resource_id,
                    account_id: String::new(),
                    workspace_id: String::new(),
                    root_id: String::new(),
                    root_path: String::new(),
                    relative_path,
                    active: true,
                    generation: 1,
                    updated_millis: 0,
                },
            );
        }
        Ok(upgraded)
    }

    pub fn persist(&self, path: &Path) -> io::Result<()> {
        if self.version != RESOURCE_REGISTRY_VERSION {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "cannot persist unsupported resource registry version",
            ));
        }
        let parent = path
            .parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "registry has no parent"))?;
        fs::create_dir_all(parent)?;
        let nonce = RESOURCE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let name = path.file_name().and_then(|value| value.to_str()).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "registry has no filename")
        })?;
        let temporary = parent.join(format!(".{name}.tmp-{}-{nonce}", std::process::id()));
        let bytes = serde_json::to_vec(self)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let result = (|| {
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&temporary)?;
            use std::io::Write;
            file.write_all(&bytes)?;
            file.sync_all()?;
            crate::platform::publish(&temporary, path)?;
            crate::platform::finish_publication(path)
        })();
        let _ = fs::remove_file(&temporary);
        result
    }

    pub fn active_for_path(
        &self,
        account_id: &str,
        workspace_id: &str,
        root_path: &str,
        relative_path: &str,
    ) -> Option<&ResourceRegistration> {
        self.entries.values().find(|entry| {
            entry.active
                && entry.account_id == account_id
                && entry.workspace_id == workspace_id
                && entry.root_path == root_path
                && entry.relative_path == relative_path
        })
    }

    pub fn active_resource(&self, resource_id: &str) -> Option<&ResourceRegistration> {
        self.entries
            .get(resource_id)
            .filter(|entry| entry.active)
    }

    pub fn validate_relative_path(relative_path: &str) -> bool {
        let path = Path::new(relative_path);
        !relative_path.is_empty()
            && !path.is_absolute()
            && path.components().all(|component| {
                matches!(component, Component::Normal(_) | Component::CurDir)
            })
    }

    pub fn canonical_root(root_path: &str, host_root: &Path) -> io::Result<PathBuf> {
        let root = fs::canonicalize(root_path)?;
        let host = fs::canonicalize(host_root)?;
        if !root.starts_with(&host) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "resource root is outside the history host root",
            ));
        }
        Ok(root)
    }

    pub fn resolve_registration(
        entry: &ResourceRegistration,
        host_root: &Path,
    ) -> io::Result<PathBuf> {
        let root = Self::canonical_root(&entry.root_path, host_root)?;
        if !Self::validate_relative_path(&entry.relative_path) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "resource registry path is invalid",
            ));
        }
        let candidate = root.join(&entry.relative_path);
        let resolved = match fs::canonicalize(&candidate) {
            Ok(path) => path,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let parent = candidate.parent().ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "resource has no parent")
                })?;
                fs::canonicalize(parent)?.join(
                    candidate
                        .file_name()
                        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "resource has no name"))?,
                )
            }
            Err(error) => return Err(error),
        };
        if !resolved.starts_with(&root) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "resource resolves outside its registered root",
            ));
        }
        Ok(resolved)
    }

    pub fn mint_id(&self) -> String {
        loop {
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos();
            let sequence = RESOURCE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let digest = blake3::hash(
                format!("{}:{}:{}", now, std::process::id(), sequence).as_bytes(),
            );
            let id = format!("file:registry:{}", digest.to_hex());
            if !self.entries.contains_key(&id) {
                return id;
            }
        }
    }
}
