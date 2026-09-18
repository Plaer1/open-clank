//! Bounded, checked Lore storage for Open Clank history.
//!
//! This package deliberately owns only the low-level content adapter. The history
//! catalog, IPC protocol and capture policy are separate S17/S18 concerns.

pub mod capture;
pub mod catalog;
pub mod operations;
pub mod protocol;
pub mod registry;
pub mod restore;
pub mod retention;
pub mod usage;

use std::fs;
use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock};

use bytes::Bytes;
use lore_storage::Context;
use lore_storage::ImmutableStore;
use lore_storage::ImmutableStoreSettings;
use lore_storage::LocalImmutableStore;
use lore_storage::Partition;
use lore_storage::ReadOptions;
use lore_storage::StoreObliterateStats;
use lore_storage::WriteContext;
use lore_storage::WriteOptions;
use lore_storage::read;
use lore_storage::write_content;

pub type StorageResult<T> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

/// The format revision owned by this adapter. Changing Lore source or patches
/// requires a deliberate format decision before opening an existing store.
pub const FORMAT_REVISION: u32 = 1;
const FORMAT_MARKER: &str = ".openclank-history-format";
static FORMAT_INIT_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
static FORMAT_TEMP_COUNTER: AtomicUsize = AtomicUsize::new(0);

pub struct HistoryStore {
    store: Arc<dyn ImmutableStore>,
    root: PathBuf,
}

impl HistoryStore {
    pub async fn open(root: impl AsRef<Path>) -> StorageResult<Self> {
        let root = root.as_ref().to_path_buf();
        Self::initialize_root(&root)?;
        let settings = ImmutableStoreSettings {
            protect_local_fragment: true,
            implicit_durable_stored: false,
            isolate_partitions: true,
            flush_background: false,
            ..Default::default()
        };
        let store = LocalImmutableStore::new(Some(root.clone()), settings).await?;
        Ok(Self { store, root })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn initialize_root(root: impl AsRef<Path>) -> io::Result<()> {
        prepare_store_root(root.as_ref())
    }

    pub async fn write(
        &self,
        partition: Partition,
        context: Context,
        content: impl Into<Bytes>,
    ) -> StorageResult<lore_storage::Address> {
        Ok(write_content(
            self.store.clone(),
            partition,
            context,
            content.into(),
            WriteOptions::default(),
            None,
            WriteContext::none(),
            None,
        )
        .await?
        .address)
    }

    pub async fn read(
        &self,
        partition: Partition,
        address: lore_storage::Address,
    ) -> StorageResult<Bytes> {
        let (_, payload) = read(
            self.store.clone(),
            partition,
            address,
            None,
            ReadOptions::default(),
            None,
        )
        .await?;
        Ok(payload)
    }

    /// A successful return is the adapter's durability receipt for pending Lore writes.
    pub async fn flush_checked(&self) -> StorageResult<()> {
        self.store.clone().flush(true).await?;
        Ok(())
    }

    /// Flush through the OS cache without claiming a durability receipt.
    pub async fn flush_relaxed(&self) -> StorageResult<()> {
        self.store.clone().flush(false).await?;
        Ok(())
    }

    pub async fn expire(
        &self,
        partition: Partition,
        address: lore_storage::Address,
    ) -> StorageResult<()> {
        self.store
            .clone()
            .obliterate(
                partition,
                address,
                Arc::new(StoreObliterateStats::default()),
            )
            .await?;
        Ok(())
    }

    pub async fn compact_checked(&self) -> StorageResult<()> {
        let mut resume = None;
        loop {
            resume = self.store.clone().compact(1, resume, true, None).await?;
            if resume.is_none() {
                return Ok(());
            }
        }
    }
}

fn prepare_store_root(root: &Path) -> io::Result<()> {
    let _guard = FORMAT_INIT_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .map_err(|_| io::Error::other("format initialization lock poisoned"))?;
    fs::create_dir_all(root)?;
    let marker = root.join(FORMAT_MARKER);
    let expected = format!("openclank-history\nformat={FORMAT_REVISION}\n").into_bytes();
    if marker.exists() {
        let contents = fs::read(&marker)?;
        if contents != expected {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "unsupported or corrupt history format marker: {}",
                    marker.display()
                ),
            ));
        }
        return sync_marker_directory(root);
    }

    let temporary_prefix = format!("{FORMAT_MARKER}.");
    let mut has_unrelated_entry = false;
    let mut marker_appeared = false;
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry.file_name();
        if name == FORMAT_MARKER {
            marker_appeared = true;
            continue;
        }
        let Some(name) = name.to_str() else {
            has_unrelated_entry = true;
            continue;
        };
        if !(name.starts_with(&temporary_prefix) && name.ends_with(".tmp")) {
            has_unrelated_entry = true;
        }
    }
    if marker_appeared {
        let contents = fs::read(&marker)?;
        if contents != expected {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "unsupported or corrupt history format marker: {}",
                    marker.display()
                ),
            ));
        }
        return sync_marker_directory(root);
    }
    if has_unrelated_entry {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "refusing unmarked non-empty history root: {}",
                root.display()
            ),
        ));
    }

    let temporary = root.join(format!(
        "{FORMAT_MARKER}.{}.{}.tmp",
        std::process::id(),
        FORMAT_TEMP_COUNTER.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        use std::io::Write;
        file.write_all(&expected)?;
        file.sync_all()?;
        match fs::hard_link(&temporary, &marker) {
            Ok(()) => sync_marker_directory(root),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let contents = fs::read(&marker)?;
                if contents == expected {
                    sync_marker_directory(root)
                } else {
                    Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "concurrent history initialization selected another format",
                    ))
                }
            }
            Err(error) => Err(error),
        }
    })();
    let _ = fs::remove_file(&temporary);
    result
}

#[cfg(unix)]
fn sync_marker_directory(root: &Path) -> io::Result<()> {
    let dir = fs::File::open(root)?;
    dir.sync_all()
}

#[cfg(not(unix))]
fn sync_marker_directory(_root: &Path) -> io::Result<()> {
    Ok(())
}
