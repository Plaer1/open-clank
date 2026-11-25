//! Approved analysis components under their own Lore context and expiry journal.
use crate::catalog::{CatalogResult, LoreRef};
use crate::operations::{unique_context, HistoryCoordinator};
use bytes::Bytes;
use lore_storage::{Address, Context, Partition};
use redb::{ReadableTable, TableDefinition, WriteTransaction};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::ops::Bound::{Excluded, Included};

const COMPONENTS: TableDefinition<&str, &[u8]> = TableDefinition::new("history_memery_components_v1");
const SWEEP: TableDefinition<&str, &str> = TableDefinition::new("history_memery_component_sweep_v1");
pub const PART: usize = 256 * 1024;
pub const MAX_SIZE: usize = 64 * 1024 * 1024;

pub(crate) fn initialize(write: &WriteTransaction) -> CatalogResult<()> {
    write.open_table(COMPONENTS)?;
    write.open_table(SWEEP)?;
    Ok(())
}

#[derive(Serialize, Deserialize)]
struct Component {
    change: String,
    digest: String,
    size: usize,
    relative_path: String,
    refs: Vec<LoreRef>,
    ready: bool,
}

fn prefix(account: &str) -> String { format!("{:x}:", Sha256::digest(account.as_bytes())) }

fn key(account: &str, change: &str, digest: &str, resource: &str) -> CatalogResult<String> {
    if [change, digest, resource.strip_prefix("resource-").unwrap_or("")].iter().any(|s| s.len() != 64 || !s.bytes().all(|v| v.is_ascii_hexdigit())) {
        return Err("invalid component identity".into());
    }
    Ok(format!("{}{change}:{resource}:{digest}", prefix(account)))
}

impl HistoryCoordinator {
    fn component_row(&self, key: &str) -> CatalogResult<Option<Component>> {
        let read = self.catalog.db.begin_read()?;
        let table = read.open_table(COMPONENTS)?;
        Ok(table.get(key)?.map(|v| serde_json::from_slice(v.value())).transpose()?)
    }

    fn save_component_row(&self, key: &str, value: &Component) -> CatalogResult<()> {
        let write = self.catalog.db.begin_write()?;
        write.open_table(COMPONENTS)?.insert(key, serde_json::to_vec(value)?.as_slice())?;
        write.commit()?;
        Ok(())
    }

    fn retained_analysis(&self, account: &str, change: &str) -> CatalogResult<bool> {
        let policy = self.policy_set()?.capture_for(account);
        let now = chrono::Utc::now().timestamp_millis() as u64;
        let minimum = now.saturating_sub(policy.max_age_days.unwrap_or(u64::MAX).saturating_mul(86_400_000));
        Ok(self.catalog.memery_manifest(account, crate::catalog::MemeryCollection::RecordsOrAssets(false))?.entries.iter()
            .any(|item| item.change == change && item.evidence && item.at >= minimum))
    }

    pub async fn put_memery_component(&self, account: &str, change: &str, digest: &str,
        bytes: Bytes, policy_revision: u64, resource: &str, relative_path: &str) -> CatalogResult<()> {
        let key = key(account, change, digest, resource)?;
        if relative_path.is_empty() || relative_path.len() > 4096 || relative_path.chars().any(char::is_control) { return Err("invalid component path".into()); }
        let policies = self.policy_set()?;
        let policy = policies.capture_for(account);
        if policies.revision != policy_revision || !policy.enabled || !policy.memery_evidence
            || bytes.len() > MAX_SIZE || !self.retained_analysis(account, change)?
            || format!("{:x}", Sha256::digest(&bytes)) != digest {
            return Err("component capture unavailable".into());
        }
        if self.component_row(&key)?.is_some_and(|v| v.ready && v.size == bytes.len()) { return Ok(()); }
        let partition = Partition::from(self.catalog.ensure_owner_partition(account)?);
        let mut refs = Vec::new();
        for (part, chunk) in bytes.chunks(PART).enumerate() {
            let context = unique_context(&format!("memery-component:{key}"), &part.to_string());
            let address = self.store.memery_address(partition, Context::from(context), Bytes::copy_from_slice(chunk)).await?;
            refs.push(LoreRef { context, hash_hex: address.hash.to_string() });
        }
        // Persist every intended address before the first byte is written, so
        // interrupted writes remain individually discoverable for erasure.
        let mut record = Component { change: change.into(), digest: digest.into(), size: bytes.len(), relative_path: relative_path.to_owned(), refs, ready: false };
        self.save_component_row(&key, &record)?;
        for (chunk, receipt) in bytes.chunks(PART).zip(&record.refs) {
            let address = self.store.write(partition, Context::from(receipt.context), Bytes::copy_from_slice(chunk)).await?;
            if address.hash.to_string() != receipt.hash_hex { return Err("component address changed".into()); }
        }
        self.store.flush_checked().await?;
        record.ready = true;
        self.save_component_row(&key, &record)
    }

    pub fn memery_component_location(&self, account: &str, change: &str, digest: &str, resource: &str) -> CatalogResult<Option<String>> {
        if !self.retained_analysis(account, change)? { return Ok(None); }
        Ok(self.component_row(&key(account, change, digest, resource)?)?.filter(|v| v.ready).map(|v| v.relative_path))
    }

    pub async fn read_memery_component(&self, account: &str, change: &str, digest: &str, resource: &str, part: u16)
        -> CatalogResult<Option<Vec<u8>>> {
        if !self.retained_analysis(account, change)? { return Ok(None); }
        let Some(row) = self.component_row(&key(account, change, digest, resource)?)? else { return Ok(None); };
        if !row.ready { return Ok(None); }
        if row.size == 0 && part == 0 { return Ok(Some(Vec::new())); }
        let receipt = row.refs.get(usize::from(part)).ok_or("component offset unavailable")?;
        let address = Address { context: Context::from(receipt.context), hash: receipt.hash_hex.parse()? };
        let bytes = self.store.read(Partition::from(self.catalog.owner_partition(account)?), address).await?;
        if bytes.len() != PART.min(row.size.saturating_sub(usize::from(part) * PART)) {
            return Err("invalid component part".into());
        }
        Ok(Some(bytes.to_vec()))
    }

    /// One bounded catalog page per host maintenance pass. A failed cleanup
    /// retains its journal and cursor for a later retry.
    pub async fn sweep_memery_components(&self, account: &str) -> CatalogResult<()> {
        let prefix = prefix(account);
        let (rows, had_cursor) = {
            let read = self.catalog.db.begin_read()?;
            let cursor = read.open_table(SWEEP)?.get(account)?.map(|v| v.value().to_owned());
            let start = cursor.as_deref().map(Excluded).unwrap_or(Included(prefix.as_str()));
            let end = format!("{prefix}~");
            let table = read.open_table(COMPONENTS)?;
            let rows = table.range::<&str>((start, Excluded(end.as_str())))?.take(4)
                .map(|row| -> CatalogResult<_> { let (k, v) = row?;
                    Ok((k.value().to_owned(), serde_json::from_slice::<Component>(v.value())?)) })
                .collect::<CatalogResult<Vec<_>>>()?;
            (rows, cursor.is_some())
        };
        let mut last = None;
        for (key, row) in &rows {
            if !row.ready || !self.retained_analysis(account, &row.change)? {
                let partition = Partition::from(self.catalog.owner_partition(account)?);
                for receipt in &row.refs {
                    let address = Address { context: Context::from(receipt.context), hash: receipt.hash_hex.parse()? };
                    self.store.expire(partition, address).await?;
                }
                self.store.flush_checked().await?;
                let write = self.catalog.db.begin_write()?;
                write.open_table(COMPONENTS)?.remove(key.as_str())?;
                write.commit()?;
            }
            last = Some(key);
        }
        if rows.len() < 4 && !had_cursor { return Ok(()); }
        let write = self.catalog.db.begin_write()?;
        if rows.len() == 4 {
            write.open_table(SWEEP)?.insert(account, last.unwrap().as_str())?;
        } else {
            write.open_table(SWEEP)?.remove(account)?;
        }
        write.commit()?;
        Ok(())
    }
}
