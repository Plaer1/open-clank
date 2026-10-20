//! Explicit history usage accounting. Physical bytes are measured once at the store root;
//! logical scope bytes are attributed separately and are never presented as additive physical
//! allocation.

use serde::{Deserialize, Serialize};
use std::io;
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use crate::retention::{ScopeKind, ScopePolicy};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub enum MeasurementQuality {
    Allocated,
    Apparent,
    Unavailable,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct ScopeUsage {
    pub scope_id: String,
    pub logical_retained_bytes: u64,
    pub retained_version_count: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RetainedVersionUsage {
    pub version_id: String,
    pub scope_id: String,
    pub workspace_id: String,
    pub locator: Option<String>,
    pub logical_bytes: u64,
    pub physical_bytes: u64,
    pub created_millis: u64,
}

/// Choose the most-specific scope for a captured version.  Attribution is
/// made at capture time, so a later move does not rewrite historical scope
/// totals; the next capture uses the new locator.
pub fn attribute_scope(
    scopes: &[ScopePolicy],
    workspace_id: &str,
    locator: Option<&Path>,
) -> Option<String> {
    attribute_scope_for_owner(scopes, None, workspace_id, locator)
}

pub fn attribute_scope_for_owner(
    scopes: &[ScopePolicy],
    owner_account_id: Option<&str>,
    workspace_id: &str,
    locator: Option<&Path>,
) -> Option<String> {
    scopes
        .iter()
        .filter(|scope| {
            if !scope.enabled {
                return false;
            }
            match scope.kind {
                ScopeKind::Global => true,
                ScopeKind::Workspace => {
                    scope.owner_account_id.as_deref() == owner_account_id
                        && scope.workspace_id.as_deref() == Some(workspace_id)
                }
                ScopeKind::Directory => {
                    scope.owner_account_id.as_deref() == owner_account_id
                        && scope.workspace_id.as_deref().is_none_or(|id| id == workspace_id)
                        && scope.root.as_deref().is_some_and(|root| {
                            locator.is_some_and(|path| path == root || path.starts_with(root))
                        })
                }
            }
        })
        .max_by(|left, right| {
            let left_class = match left.kind {
                ScopeKind::Global => 0,
                ScopeKind::Workspace => 1,
                ScopeKind::Directory => 2,
            };
            let right_class = match right.kind {
                ScopeKind::Global => 0,
                ScopeKind::Workspace => 1,
                ScopeKind::Directory => 2,
            };
            left_class
                .cmp(&right_class)
                .then_with(|| {
                    left.root
                        .as_ref()
                        .map_or(0, |root| root.as_os_str().len())
                        .cmp(&right.root.as_ref().map_or(0, |root| root.as_os_str().len()))
                })
                .then_with(|| right.scope_id.cmp(&left.scope_id))
        })
        .map(|scope| scope.scope_id.clone())
}

pub fn aggregate_scopes(versions: &[RetainedVersionUsage]) -> Vec<ScopeUsage> {
    let mut scopes = std::collections::BTreeMap::<String, ScopeUsage>::new();
    for version in versions {
        let entry = scopes
            .entry(version.scope_id.clone())
            .or_insert_with(|| ScopeUsage {
                scope_id: version.scope_id.clone(),
                ..ScopeUsage::default()
            });
        entry.logical_retained_bytes = entry
            .logical_retained_bytes
            .saturating_add(version.logical_bytes);
        entry.retained_version_count = entry.retained_version_count.saturating_add(1);
    }
    scopes.into_values().collect()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HistoryUsage {
    pub logical_retained_bytes: u64,
    pub physical_allocated_bytes: u64,
    pub apparent_file_bytes: u64,
    pub reserved_inflight_bytes: u64,
    pub reclaimable_estimate_bytes: u64,
    pub retained_version_count: u64,
    pub measured_at_millis: u64,
    pub measurement_quality: MeasurementQuality,
    pub scopes: Vec<ScopeUsage>,
}

pub fn measure_root(root: &Path, measured_at_millis: u64) -> io::Result<HistoryUsage> {
    let mut apparent = 0u64;
    let mut allocated = 0u64;
    let mut files = 0u64;
    if root.exists() {
        measure_entry(root, &mut apparent, &mut allocated, &mut files)?;
    }
    #[cfg(unix)]
    let quality = MeasurementQuality::Allocated;
    #[cfg(not(unix))]
    let quality = MeasurementQuality::Apparent;
    Ok(HistoryUsage {
        logical_retained_bytes: apparent,
        physical_allocated_bytes: allocated,
        apparent_file_bytes: apparent,
        reserved_inflight_bytes: 0,
        reclaimable_estimate_bytes: 0,
        retained_version_count: files,
        measured_at_millis,
        measurement_quality: quality,
        scopes: Vec::new(),
    })
}

/// Re-measure after cleanup and carry the caller's durable reservation total
/// and reclaimable estimate into the status returned to Settings.
pub fn measure_root_with_reservations(
    root: &Path,
    measured_at_millis: u64,
    reserved_inflight_bytes: u64,
    reclaimable_estimate_bytes: u64,
    versions: &[RetainedVersionUsage],
) -> io::Result<HistoryUsage> {
    let mut usage = measure_root(root, measured_at_millis)?;
    usage.reserved_inflight_bytes = reserved_inflight_bytes;
    usage.reclaimable_estimate_bytes = reclaimable_estimate_bytes;
    usage.logical_retained_bytes = versions.iter().fold(0u64, |sum, version| {
        sum.saturating_add(version.logical_bytes)
    });
    usage.retained_version_count = versions.len() as u64;
    usage.scopes = aggregate_scopes(versions);
    Ok(usage)
}

pub fn add_external_storage(usage: &mut HistoryUsage, path: &Path) -> io::Result<()> {
    let mut apparent = 0u64;
    let mut allocated = 0u64;
    let mut files = 0u64;
    if path.exists() {
        measure_entry(path, &mut apparent, &mut allocated, &mut files)?;
    }
    usage.apparent_file_bytes = usage.apparent_file_bytes.saturating_add(apparent);
    usage.physical_allocated_bytes = usage.physical_allocated_bytes.saturating_add(allocated);
    Ok(())
}

fn measure_entry(
    path: &Path,
    apparent: &mut u64,
    allocated: &mut u64,
    files: &mut u64,
) -> io::Result<()> {
    let metadata = std::fs::symlink_metadata(path)?;
    if metadata.is_dir() {
        for entry in std::fs::read_dir(path)? {
            measure_entry(&entry?.path(), apparent, allocated, files)?;
        }
        return Ok(());
    }
    *apparent = apparent.saturating_add(metadata.len());
    *files = files.saturating_add(1);
    #[cfg(unix)]
    {
        *allocated = allocated.saturating_add(metadata.blocks().saturating_mul(512));
    }
    #[cfg(not(unix))]
    {
        *allocated = allocated.saturating_add(metadata.len());
    }
    Ok(())
}
