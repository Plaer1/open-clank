use bytes::Bytes;
use lore_storage::Context;
use lore_storage::Partition;
use lore_storage::inject_directory_sync_failures;
use lore_storage::inject_index_serialization_failures;
use lore_storage::inject_sync_data_failures;
use openclank_history::HistoryStore;
use std::sync::Mutex;

static TEST_LOCK: Mutex<()> = Mutex::new(());

fn disk_bytes(root: &std::path::Path) -> u64 {
    fn visit(path: &std::path::Path, total: &mut u64) {
        for entry in std::fs::read_dir(path).unwrap() {
            let entry = entry.unwrap();
            let metadata = entry.metadata().unwrap();
            if metadata.is_dir() {
                visit(&entry.path(), total);
            } else {
                *total += metadata.len();
            }
        }
    }
    let mut total = 0;
    visit(root, &mut total);
    total
}

fn logical_pack_bytes(root: &std::path::Path) -> u64 {
    fn visit(path: &std::path::Path, total: &mut u64) {
        for entry in std::fs::read_dir(path).unwrap() {
            let entry = entry.unwrap();
            let metadata = entry.metadata().unwrap();
            if metadata.is_dir() {
                if entry.file_name() == "pack" {
                    *total += disk_bytes(&entry.path());
                } else {
                    visit(&entry.path(), total);
                }
            }
        }
    }
    let mut total = 0;
    visit(root, &mut total);
    total
}

#[cfg(unix)]
fn allocated_pack_bytes(root: &std::path::Path) -> u64 {
    use std::os::unix::fs::MetadataExt;
    fn visit(path: &std::path::Path, total: &mut u64, files: &mut usize) {
        for entry in std::fs::read_dir(path).unwrap() {
            let entry = entry.unwrap();
            let metadata = entry.metadata().unwrap();
            if metadata.is_dir() {
                visit(&entry.path(), total, files);
            } else if path.file_name().is_some_and(|name| name == "pack") {
                *total += metadata.blocks() * 512;
                *files += 1;
            }
        }
    }
    let mut total = 0;
    let mut files = 0;
    visit(root, &mut total, &mut files);
    assert!(
        files > 0,
        "expected at least one file in immutable/index/<group>/pack"
    );
    total
}

#[tokio::test]
async fn format_marker_is_required_and_version_checked_before_lore_open() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let marker = dir.path().join(".openclank-history-format");
    let first = HistoryStore::open(dir.path()).await.unwrap();
    drop(first);
    assert_eq!(
        std::fs::read_to_string(&marker).unwrap(),
        "openclank-history\nformat=1\n"
    );

    let future = tempfile::tempdir().unwrap();
    std::fs::write(
        future.path().join(".openclank-history-format"),
        "openclank-history\nformat=999\n",
    )
    .unwrap();
    let future_before = std::fs::read(future.path().join(".openclank-history-format")).unwrap();
    assert_eq!(std::fs::read_dir(future.path()).unwrap().count(), 1);
    assert!(HistoryStore::open(future.path()).await.is_err());
    assert_eq!(
        std::fs::read(future.path().join(".openclank-history-format")).unwrap(),
        future_before
    );

    let unmarked = tempfile::tempdir().unwrap();
    std::fs::write(unmarked.path().join("unrelated"), b"do not adopt").unwrap();
    let unmarked_before = std::fs::read(unmarked.path().join("unrelated")).unwrap();
    assert!(HistoryStore::open(unmarked.path()).await.is_err());
    assert_eq!(std::fs::read_dir(unmarked.path()).unwrap().count(), 1);
    assert_eq!(
        std::fs::read(unmarked.path().join("unrelated")).unwrap(),
        unmarked_before
    );

    let concurrent = tempfile::tempdir().unwrap();
    let fixture = env!("CARGO_BIN_EXE_lore_fixture");
    let mut first = std::process::Command::new(fixture)
        .args(["marker-only", concurrent.path().to_str().unwrap()])
        .spawn()
        .unwrap();
    let mut second = std::process::Command::new(fixture)
        .args(["marker-only", concurrent.path().to_str().unwrap()])
        .spawn()
        .unwrap();
    assert!(first.wait().unwrap().success());
    assert!(second.wait().unwrap().success());
    assert_eq!(
        std::fs::read_to_string(concurrent.path().join(".openclank-history-format")).unwrap(),
        "openclank-history\nformat=1\n"
    );
}

#[tokio::test]
async fn expiry_compaction_preserves_deduplicated_survivor() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let history = HistoryStore::open(dir.path()).await.unwrap();
    let partition = Partition::from([1; 16]);
    let expired_context = Context::from([1; 16]);
    let survivor_context = Context::from([9; 16]);
    let payload = Bytes::from_static(b"# Copal\n- [ ] learner task\n");

    let expired = history
        .write(partition, expired_context, payload.clone())
        .await
        .unwrap();
    let survivor = history
        .write(partition, survivor_context, payload.clone())
        .await
        .unwrap();
    assert_eq!(expired.hash, survivor.hash);
    history.flush_checked().await.unwrap();
    history.expire(partition, expired).await.unwrap();
    history.flush_checked().await.unwrap();
    history.compact_checked().await.unwrap();

    assert_eq!(history.read(partition, survivor).await.unwrap(), payload);
    assert!(history.read(partition, expired).await.is_err());
}

#[tokio::test]
async fn expiry_compaction_reclaims_physical_pack_bytes() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let history = HistoryStore::open(dir.path()).await.unwrap();
    let partition = Partition::from([15; 16]);
    let mut first = vec![0u8; 256 * 1024];
    let mut state = 0x1234_5678u32;
    for byte in &mut first {
        state ^= state << 13;
        state ^= state >> 17;
        state ^= state << 5;
        *byte = state as u8;
    }
    let mut second = first.clone();
    second[0] ^= 0xff;
    let expired = history
        .write(partition, Context::from([1; 16]), Bytes::from(first))
        .await
        .unwrap();
    let survivor = history
        .write(partition, Context::from([2; 16]), Bytes::from(second))
        .await
        .unwrap();
    history.flush_checked().await.unwrap();
    let before = logical_pack_bytes(dir.path());
    #[cfg(unix)]
    let allocated_before = allocated_pack_bytes(dir.path());
    assert!(before > 0, "expected a real index/<group>/pack tree");
    history.expire(partition, expired).await.unwrap();
    history.flush_checked().await.unwrap();
    history.compact_checked().await.unwrap();
    let after = logical_pack_bytes(dir.path());
    #[cfg(unix)]
    let allocated_after = allocated_pack_bytes(dir.path());
    eprintln!("pack reclamation logical bytes: before={before}, after={after}");
    #[cfg(unix)]
    eprintln!(
        "pack reclamation allocated bytes: before={allocated_before}, after={allocated_after}"
    );
    assert!(
        after < before,
        "compaction did not reclaim bytes: before={before}, after={after}"
    );
    #[cfg(unix)]
    assert!(
        allocated_after <= allocated_before,
        "allocated bytes grew: before={allocated_before}, after={allocated_after}"
    );
    assert_eq!(
        history.read(partition, survivor).await.unwrap().len(),
        256 * 1024
    );
}

#[tokio::test]
async fn configured_partition_isolation_rejects_foreign_reads() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let history = HistoryStore::open(dir.path()).await.unwrap();
    let owner = Partition::from([1; 16]);
    let foreign = Partition::from([2; 16]);
    let address = history
        .write(
            owner,
            Context::from([3; 16]),
            Bytes::from_static(b"private"),
        )
        .await
        .unwrap();
    history.flush_checked().await.unwrap();
    assert!(history.read(foreign, address).await.is_err());
}

#[tokio::test]
async fn checked_flush_survives_reopen_for_mixed_payloads() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let partition = Partition::from([7; 16]);
    let entries = [
        (Context::from([1; 16]), Bytes::from_static(b"markdown")),
        (Context::from([2; 16]), Bytes::from(vec![0x5a; 256 * 1024])),
        (
            Context::from([3; 16]),
            Bytes::from_static(br#"{"properties":{"done":true},"relations":[]}"#),
        ),
    ];
    let mut addresses = Vec::new();
    {
        let history = HistoryStore::open(dir.path()).await.unwrap();
        for (context, content) in &entries {
            addresses.push(
                history
                    .write(partition, *context, content.clone())
                    .await
                    .unwrap(),
            );
        }
        history.flush_checked().await.unwrap();
    }

    let reopened = HistoryStore::open(dir.path()).await.unwrap();
    for ((_, expected), address) in entries.iter().zip(addresses) {
        assert_eq!(reopened.read(partition, address).await.unwrap(), *expected);
    }
}

#[tokio::test]
async fn failed_pack_sync_and_index_write_are_checked_and_retryable() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let history = HistoryStore::open(dir.path()).await.unwrap();
    let partition = Partition::from([8; 16]);
    let address = history
        .write(
            partition,
            Context::from([4; 16]),
            Bytes::from_static(b"retryable durability"),
        )
        .await
        .unwrap();

    inject_sync_data_failures(1);
    assert!(history.flush_checked().await.is_err());
    history.flush_checked().await.unwrap();

    let address2 = history
        .write(
            partition,
            Context::from([5; 16]),
            Bytes::from_static(b"retryable index"),
        )
        .await
        .unwrap();
    inject_index_serialization_failures(1);
    assert!(history.flush_checked().await.is_err());
    history.flush_checked().await.unwrap();

    assert_eq!(
        history.read(partition, address).await.unwrap().as_ref(),
        b"retryable durability"
    );
    assert_eq!(
        history.read(partition, address2).await.unwrap().as_ref(),
        b"retryable index"
    );
}

#[tokio::test]
async fn relaxed_flush_keeps_file_backed_pack_dirty_for_checked_retry() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let history = HistoryStore::open(dir.path()).await.unwrap();
    let partition = Partition::from([13; 16]);
    let address = history
        .write(
            partition,
            Context::from([1; 16]),
            Bytes::from_static(b"relaxed then checked"),
        )
        .await
        .unwrap();
    history.flush_relaxed().await.unwrap();
    inject_sync_data_failures(1);
    assert!(history.flush_checked().await.is_err());
    history.flush_checked().await.unwrap();
    drop(history);
    let reopened = HistoryStore::open(dir.path()).await.unwrap();
    assert_eq!(
        reopened.read(partition, address).await.unwrap().as_ref(),
        b"relaxed then checked"
    );
}

#[tokio::test]
async fn directory_sync_failure_is_checked_dirty_and_retryable() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let history = HistoryStore::open(dir.path()).await.unwrap();
    let partition = Partition::from([14; 16]);
    let address = history
        .write(
            partition,
            Context::from([1; 16]),
            Bytes::from_static(b"directory receipt"),
        )
        .await
        .unwrap();
    inject_directory_sync_failures(1);
    assert!(history.flush_checked().await.is_err());
    history.flush_checked().await.unwrap();
    drop(history);
    let reopened = HistoryStore::open(dir.path()).await.unwrap();
    assert_eq!(
        reopened.read(partition, address).await.unwrap().as_ref(),
        b"directory receipt"
    );
}

#[tokio::test]
async fn compaction_publication_failure_precedes_retirement_and_retries_after_reopen() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let partition = Partition::from([12; 16]);
    let history = HistoryStore::open(dir.path()).await.unwrap();
    let expired = history
        .write(
            partition,
            Context::from([1; 16]),
            Bytes::from_static(b"compact survivor"),
        )
        .await
        .unwrap();
    let survivor = history
        .write(
            partition,
            Context::from([2; 16]),
            Bytes::from_static(b"compact survivor"),
        )
        .await
        .unwrap();
    history.flush_checked().await.unwrap();
    history.expire(partition, expired).await.unwrap();
    history.flush_checked().await.unwrap();

    inject_sync_data_failures(1);
    assert!(history.compact_checked().await.is_err());
    drop(history);

    let reopened = HistoryStore::open(dir.path()).await.unwrap();
    reopened.compact_checked().await.unwrap();
    assert_eq!(
        reopened.read(partition, survivor).await.unwrap().as_ref(),
        b"compact survivor"
    );
    assert!(reopened.read(partition, expired).await.is_err());
}

#[test]
fn checked_flush_then_process_abort_has_exact_fresh_reader_bytes() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let writer = std::process::Command::new(env!("CARGO_BIN_EXE_lore_fixture"))
        .args(["write-abort", dir.path().to_str().unwrap()])
        .status()
        .unwrap();
    assert!(!writer.success());
    let reader = std::process::Command::new(env!("CARGO_BIN_EXE_lore_fixture"))
        .args(["read", dir.path().to_str().unwrap()])
        .status()
        .unwrap();
    assert!(reader.success());
}

#[test]
fn process_abort_during_compaction_preserves_fresh_reader_addresses() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let fixture = env!("CARGO_BIN_EXE_lore_fixture");
    assert!(
        std::process::Command::new(fixture)
            .args(["prepare-compaction", dir.path().to_str().unwrap()])
            .status()
            .unwrap()
            .success()
    );
    for phase in ["1", "2"] {
        let aborted = std::process::Command::new(fixture)
            .args(["compact-abort", dir.path().to_str().unwrap(), phase])
            .status()
            .unwrap();
        assert!(!aborted.success(), "compaction phase {phase} did not abort");
        assert!(
            std::process::Command::new(fixture)
                .args(["read-compaction", dir.path().to_str().unwrap()])
                .status()
                .unwrap()
                .success()
        );
        assert!(
            std::process::Command::new(fixture)
                .args(["compact", dir.path().to_str().unwrap()])
                .status()
                .unwrap()
                .success()
        );
    }
}
