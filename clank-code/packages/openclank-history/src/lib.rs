//! Bounded, checked Lore storage for Open Clank history.
//!
//! This package deliberately owns only the low-level content adapter. The history
//! catalog, IPC protocol and capture policy are separate S17/S18 concerns.

pub mod platform;
pub mod migration_copy;
#[cfg(unix)] pub mod migration_catalog;
pub mod capture;
pub mod catalog;
pub mod operations;
pub mod memery_components;
pub mod protocol;
pub mod registry;
pub mod restore;
pub mod retention;
pub mod usage;
pub mod tree_container;

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
pub const FORMAT_REVISION: u32 = 3;
pub(crate) const LORE_SOURCE: &str = "317f48b918f0b4d43de104d99e80ac4a57b697df";
pub(crate) const LORE_PATCHSET: &str = "openclank-v014-1";
pub(crate) const FORMAT_MARKER: &str = ".openclank-history-format";
static FORMAT_INIT_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
static FORMAT_TEMP_COUNTER: AtomicUsize = AtomicUsize::new(0);

#[derive(Clone)]
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

    /// Digest a retained object once with a bounded channel. The caller owns
    /// the Catalog lease until this future and the spawned Lore producer drain.
    pub async fn digest_bounded(&self, partition: Partition, address: lore_storage::Address, expected: u64) -> StorageResult<String> {
        use sha2::{Digest, Sha256};
        if expected > 64 * 1024 * 1024 { return Err("version too large".into()); }
        let (sender, mut receiver) = tokio::sync::mpsc::channel(2);
        let (_, range) = lore_storage::read_stream(self.store.clone(), partition, address, None,
            ReadOptions::default().with_strict_local().with_max_content_size(64 * 1024 * 1024), sender, None).await?;
        let mut invalid = range != (0..expected); let mut hash = Sha256::new(); let mut count=0u64;
        while let Some(part) = receiver.recv().await {
            match part {
                Ok(data) => match count.checked_add(data.len() as u64) {
                    Some(next) if next <= expected => { count=next; hash.update(&data); },
                    _ => invalid=true,
                },
                Err(_) => invalid=true,
            }
        }
        if invalid || count != expected { return Err("version incomplete or changed".into()); }
        Ok(format!("{:x}",hash.finalize()))
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn initialize_root(root: impl AsRef<Path>) -> io::Result<()> {
        prepare_store_root(root.as_ref())?;
        validate_physical_layout(root.as_ref())
    }

    pub(crate) async fn memery_address(
        &self, partition: Partition, context: Context, content: Bytes,
    ) -> StorageResult<lore_storage::Address> {
        Ok(write_content(self.store.clone(), partition, context, content,
            WriteOptions::default().hash_only(), None, WriteContext::none(), None).await?.address)
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
            ReadOptions::default().with_strict_local(),
            None,
        )
        .await?;
        Ok(payload)
    }

    /// Write exactly one already-validated, pinned worktree tree as one Lore root.
    /// No catalog action or authority is created here; callers must own both.
    #[cfg(target_os = "linux")]
    pub(crate) async fn write_tree_pinned(
        &self, partition: Partition, context: Context, file: &std::fs::File,
    ) -> StorageResult<(lore_storage::Address, tree_container::Summary)> {
        use std::os::fd::AsRawFd;
        let (_, expected) = tree_container::validate_pinned(file)?;
        let path = PathBuf::from(format!("/proc/self/fd/{}", file.as_raw_fd()));
        let source = lore_storage::ContentSource::file(&path);
        let written = lore_storage::write_from_file(
            self.store.clone(), partition, &source, context, WriteOptions::default(), None, WriteContext::none(),
        ).await?;
        if written.size_content != expected.bytes || tree_container::validate_pinned(file)?.1 != expected {
            return Err("tree stage changed during capture".into());
        }
        self.verify_tree_stream(partition, written.address, &expected).await?;
        self.flush_checked().await?;
        Ok((written.address, expected))
    }

    /// Consume the complete Lore stream; a short or failed tail is never success.
    #[cfg(target_os = "linux")]
    async fn verify_tree_stream(&self, partition: Partition, address: lore_storage::Address,
        expected: &tree_container::Summary) -> StorageResult<()> {
        use sha2::{Digest, Sha256};
        let (sender, mut receiver) = tokio::sync::mpsc::channel(2);
        let (_, range) = lore_storage::read::read_stream(self.store.clone(), partition, address, None,
            ReadOptions::default().with_strict_local(), sender, None).await?;
        if range.start != 0 || range.end != expected.bytes { return Err("tree stream length changed".into()); }
        let mut count=0u64;let mut digest=Sha256::new();
        while let Some(chunk)=receiver.recv().await {
            let chunk=chunk?;count=count.checked_add(chunk.len() as u64).ok_or("tree stream overflow")?;
            if count>expected.bytes {return Err("tree stream overflow".into());}
            digest.update(&chunk);
        }
        if count!=expected.bytes || format!("{:x}",digest.finalize())!=expected.container_sha256 {
            return Err("tree stream integrity unavailable".into());
        }
        Ok(())
    }

    /// Read one retained root into a fresh pinned stage; partial stages remain
    /// uncommitted and must never be published by the caller.
    #[cfg(target_os = "linux")]
    pub(crate) async fn read_tree_to_pinned_stage(&self, partition: Partition, address: lore_storage::Address,
        expected: &tree_container::Summary, file: &std::fs::File) -> StorageResult<()> {
        use sha2::{Digest, Sha256};
        use std::os::unix::fs::{FileExt, MetadataExt};
        let before=file.metadata()?;
        if !before.is_file() || before.nlink()!=1 || before.len()!=0 || expected.bytes>tree_container::MAX_TOTAL+tree_container::MAX_MANIFEST as u64+12 {
            return Err("tree destination stage unavailable".into());
        }
        let (sender, mut receiver)=tokio::sync::mpsc::channel(2);
        let (_,range)=lore_storage::read::read_stream(self.store.clone(),partition,address,None,ReadOptions::default().with_strict_local(),sender,None).await?;
        if range.start!=0 || range.end!=expected.bytes {return Err("tree stream length changed".into());}
        let mut offset=0u64;let mut digest=Sha256::new();
        while let Some(chunk)=receiver.recv().await {
            let chunk=chunk?;let next=offset.checked_add(chunk.len() as u64).ok_or("tree stream overflow")?;
            if next>expected.bytes {return Err("tree stream overflow".into());}
            file.write_all_at(&chunk,offset)?;digest.update(&chunk);offset=next;
        }
        if offset!=expected.bytes || format!("{:x}",digest.finalize())!=expected.container_sha256 {
            return Err("tree stream integrity unavailable".into());
        }
        file.sync_all()?;
        let after=file.metadata()?;
        if before.dev()!=after.dev() || before.ino()!=after.ino() || after.nlink()!=1 || tree_container::validate_pinned(file)?.1!=*expected {
            return Err("tree destination stage changed".into());
        }
        Ok(())
    }

    /// A successful return is the adapter's durability receipt for pending Lore writes.
    pub async fn flush_checked(&self) -> StorageResult<()> {
        self.store.clone().flush(lore_storage::FlushOptions::SYNC_DATA).await?;
        Ok(())
    }

    /// Flush through the OS cache without claiming a durability receipt.
    pub async fn flush_relaxed(&self) -> StorageResult<()> {
        self.store.clone().flush(lore_storage::FlushOptions::default()).await?;
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

/// Lore can upgrade older pack/bucket layouts during open or later flush.
/// Admit only layouts that its current reader preserves without conversion.
pub(crate) fn validate_physical_layout(root: &Path) -> io::Result<()> {
    let immutable = root.join("immutable");
    let refuse = |path: &Path| io::Error::new(io::ErrorKind::InvalidData,
        format!("legacy or incompatible Lore layout {}; use an explicit offline history conversion under .clanker/tools", path.display()));
    if immutable.join("pack").exists() { return Err(refuse(&immutable.join("pack"))); }
    let mut pending = vec![immutable.join("index")];
    while let Some(dir) = pending.pop() {
        let entries = match fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        for entry in entries {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_dir() { pending.push(entry.path()); continue; }
            if !entry.file_name().to_string_lossy().starts_with("index_") { continue; }
            use std::io::Read;
            let mut bytes = [0u8;4];
            fs::File::open(entry.path())?.read_exact(&mut bytes)?;
            let version = u32::from_le_bytes(bytes);
            if version != 4 && version != 5 { return Err(refuse(&entry.path())); }
        }
    }
    Ok(())
}

#[cfg(unix)]
pub(crate) fn supported_copy_source(marker: &[u8]) -> bool {
    marker == b"openclank-history\nformat=1\n"
        || marker == b"openclank-history\nformat=2\nlore-source=f00dbbf07cb449103e211ccbecaa995630f47d2d\npatchset=openclank-v012-p04-1\n"
}

pub(crate) fn canonical_format_marker() -> Vec<u8> {
    format!("openclank-history\nformat={FORMAT_REVISION}\nlore-source={LORE_SOURCE}\npatchset={LORE_PATCHSET}\n").into_bytes()
}

fn prepare_store_root(root: &Path) -> io::Result<()> {
    let _guard = FORMAT_INIT_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .map_err(|_| io::Error::other("format initialization lock poisoned"))?;
    fs::create_dir_all(root)?;
    let marker = root.join(FORMAT_MARKER);
    let expected = canonical_format_marker();
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

fn sync_marker_directory(root: &Path) -> io::Result<()> { platform::finish_publication(&root.join(FORMAT_MARKER)) }
