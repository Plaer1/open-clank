use crate::{build_id, schema_sha256, SupervisorError};
use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InstanceManifest {
    pub instance_id: String,
    pub pid: u32,
    pub process_start_token: String,
    pub transport_kind: String,
    pub socket_device: Option<u64>,
    pub socket_inode: Option<u64>,
    pub windows_loopback_port: Option<u16>,
    pub endpoint_identity_sha256: String,
    pub started_unix_ms: u128,
    pub heartbeat_unix_ms: u128,
    pub build_id: String,
    pub schema_sha256: String,
}

pub struct RuntimeRootGuard {
    root: PathBuf,
    lock_path: PathBuf,
    manifest_path: PathBuf,
    _lock: File,
}

impl RuntimeRootGuard {
    pub fn acquire(
        root: impl AsRef<Path>,
        instance_id: impl Into<String>,
        transport_kind: impl Into<String>,
        endpoint_identity_sha256: impl Into<String>,
    ) -> Result<Self, SupervisorError> {
        let root = crate::validate_runtime_root(root.as_ref())?;
        fs::create_dir_all(&root)?;
        set_private_dir(&root)?;
        let lock_path = root.join("runtime-root.lock");
        let lock = match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock_path)
        {
            Ok(lock) => lock,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                return Err(SupervisorError::RuntimeRootInUse)
            }
            Err(error) => return Err(error.into()),
        };
        set_private_file(&lock_path)?;

        let instance_id = instance_id.into();
        let manifest_dir = root.join("instances");
        if let Err(error) =
            fs::create_dir_all(&manifest_dir).and_then(|_| set_private_dir(&manifest_dir))
        {
            let _ = fs::remove_file(&lock_path);
            return Err(error.into());
        }
        let manifest_path = manifest_dir.join(format!("{instance_id}.json"));
        let started_unix_ms = now_unix_ms();
        let manifest = InstanceManifest {
            instance_id,
            pid: std::process::id(),
            process_start_token: format!("{}-{started_unix_ms}", std::process::id()),
            transport_kind: transport_kind.into(),
            socket_device: None,
            socket_inode: None,
            windows_loopback_port: None,
            endpoint_identity_sha256: endpoint_identity_sha256.into(),
            started_unix_ms,
            heartbeat_unix_ms: started_unix_ms,
            build_id: build_id().to_owned(),
            schema_sha256: schema_sha256(),
        };
        if let Err(error) = write_manifest(&manifest_path, &manifest) {
            let _ = fs::remove_file(&lock_path);
            return Err(error);
        }
        Ok(Self {
            root,
            lock_path,
            manifest_path,
            _lock: lock,
        })
    }

    pub fn manifest_path(&self) -> &Path {
        &self.manifest_path
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn heartbeat(&self) -> Result<(), SupervisorError> {
        let mut manifest: InstanceManifest =
            serde_json::from_slice(&fs::read(&self.manifest_path)?)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        manifest.heartbeat_unix_ms = now_unix_ms();
        write_manifest(&self.manifest_path, &manifest)
    }
}

impl Drop for RuntimeRootGuard {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.manifest_path);
        let _ = fs::remove_file(&self.lock_path);
    }
}

fn now_unix_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn write_manifest(path: &Path, manifest: &InstanceManifest) -> Result<(), SupervisorError> {
    let bytes = serde_json::to_vec_pretty(manifest)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let temp = path.with_extension("json.tmp");
    fs::write(&temp, bytes)?;
    set_private_file(&temp)?;
    fs::rename(temp, path)?;
    set_private_file(path)?;
    Ok(())
}

fn set_private_dir(path: &Path) -> Result<(), io::Error> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn set_private_file(path: &Path) -> Result<(), io::Error> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_runtime_root_is_exclusive_and_manifest_is_safe() {
        let root = tempfile::tempdir().expect("tempdir");
        let guard = RuntimeRootGuard::acquire(root.path(), "one", "unix_uds", "identity")
            .expect("first owner");
        assert!(matches!(
            RuntimeRootGuard::acquire(root.path(), "two", "unix_uds", "identity"),
            Err(SupervisorError::RuntimeRootInUse)
        ));
        let text = fs::read_to_string(guard.manifest_path()).expect("manifest");
        assert!(!text.contains("supervisor.sock"));
        assert!(!text.contains("credential"));
        guard.heartbeat().expect("heartbeat");
    }

    #[test]
    fn distinct_runtime_roots_can_coexist() {
        let left = tempfile::tempdir().expect("left");
        let right = tempfile::tempdir().expect("right");
        let _left = RuntimeRootGuard::acquire(left.path(), "one", "unix_uds", "a").expect("left");
        let _right =
            RuntimeRootGuard::acquire(right.path(), "two", "unix_uds", "b").expect("right");
    }
}
