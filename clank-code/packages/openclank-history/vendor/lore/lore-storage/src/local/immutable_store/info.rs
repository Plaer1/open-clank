// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT

use std::path::Path;
use std::path::PathBuf;

use zerocopy::FromBytes;
use zerocopy::FromZeros;
use zerocopy::Immutable;
use zerocopy::IntoBytes;

use crate::LocalImmutableStoreError;

/// Magic value at the start of the info file. Bytes spell `IS_I` on a little-endian target.
#[lore_macro::test_pub]
const INFO_MAGIC: u32 = u32::from_le_bytes(*b"IS_I");

/// Bumped only if the file's binary layout changes.
#[lore_macro::test_pub]
#[repr(u32)]
enum ImmutableStoreInfoVersion {
    Initial = 0,
}

#[lore_macro::test_pub]
#[repr(C)]
#[derive(Debug, IntoBytes, FromBytes, Immutable)]
pub struct ImmutableStoreInfo {
    magic: u32,
    version: u32,

    /// What is the next group that needs migrated from local Oodle data?
    /// -1 is if there is no more migration to be done.
    ///
    /// Descending: the number of materialized groups can only increase with store usage,
    /// and groups that are newly materialized after this index
    /// are known to never contain Oodle since it is no longer allowed by `lore_storage`.
    /// Therefore, groups are migrated from N down.
    pub next_group_index_to_migrate_oodle: i32,
}

impl Default for ImmutableStoreInfo {
    fn default() -> Self {
        Self {
            magic: INFO_MAGIC,
            version: ImmutableStoreInfoVersion::Initial as u32,
            next_group_index_to_migrate_oodle: -1,
        }
    }
}

struct ImmutableStoreInfoSegment(Box<[u8; size_of::<ImmutableStoreInfo>()]>);

impl lore_io::StableBufList for ImmutableStoreInfoSegment {
    fn byte_segments(&self) -> impl Iterator<Item = &[u8]> {
        std::iter::once(self.0.as_ref().as_slice())
    }
}

pub fn info_path_for_store_root(path: &Path) -> PathBuf {
    path.join("info")
}

pub async fn read_info_file(path: &Path) -> std::io::Result<Option<ImmutableStoreInfo>> {
    let bytes = match lore_io::IoDriver::global().read_file_bytes(path).await {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    let mut header = ImmutableStoreInfo::new_zeroed();
    let expected = size_of::<ImmutableStoreInfo>();
    if bytes.len() < expected {
        return Err(std::io::Error::new(
            std::io::ErrorKind::UnexpectedEof,
            format!("Info file is {} bytes, expected {expected}", bytes.len()),
        ));
    }
    header.as_mut_bytes().copy_from_slice(&bytes[..expected]);
    if header.magic != INFO_MAGIC {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!(
                "Info file has invalid magic 0x{:08x}, expected 0x{:08x}",
                header.magic, INFO_MAGIC
            ),
        ));
    }
    if header.version != ImmutableStoreInfoVersion::Initial as u32 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!(
                "Info file has unsupported version {}, expected {}",
                header.version,
                ImmutableStoreInfoVersion::Initial as u32
            ),
        ));
    }

    Ok(Some(header))
}

/// Replace the info file at `path`. The write is atomic and durable: a torn one would leave a
/// file that cannot be read, and the store cannot be opened without it.
pub async fn write_info_file(info: &ImmutableStoreInfo, path: &Path) -> std::io::Result<()> {
    let mut segment = Box::new([0u8; size_of::<ImmutableStoreInfo>()]);
    segment.copy_from_slice(info.as_bytes());
    lore_io::IoDriver::global()
        .write_file_segments_atomic(
            path.with_extension("tmp"),
            path,
            &lore_io::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true),
            ImmutableStoreInfoSegment(segment),
            true,
            (),
        )
        .await?;
    Ok(())
}

pub async fn get_or_init_disk_info(
    path: &Path,
    next_group_index_to_migrate_oodle: i32,
) -> Result<ImmutableStoreInfo, LocalImmutableStoreError> {
    let info_file_path = info_path_for_store_root(path);

    if let Some(info_from_disk) = read_info_file(info_file_path.as_path())
        .await
        .map_err(|err| {
            LocalImmutableStoreError::internal_with_context(err, "Failed to load store info file")
        })?
    {
        return Ok(info_from_disk);
    }

    let mut new_info = ImmutableStoreInfo::default();
    if next_group_index_to_migrate_oodle != -1 {
        new_info.next_group_index_to_migrate_oodle = next_group_index_to_migrate_oodle;
    }
    write_info_file(&new_info, info_file_path.as_path())
        .await
        .map_err(|err| {
            LocalImmutableStoreError::internal_with_context(
                err,
                "Failed to store initial info file",
            )
        })?;

    Ok(new_info)
}
