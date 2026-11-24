use bytes::Bytes;
use lore_storage::{Context, Partition};
use openclank_history::HistoryStore;
use openclank_history::operations::HistoryCoordinator;

#[tokio::test]
async fn new_root_durable_partition_and_old_root_refusal() {
    let old = tempfile::tempdir().unwrap();
    let marker = old.path().join(".openclank-history-format");
    std::fs::write(&marker, b"openclank-history\nformat=1\n").unwrap();
    std::fs::create_dir(old.path().join("immutable")).unwrap();
    let sentinel = old.path().join("immutable/old-data");
    std::fs::write(&sentinel, b"preserve old data").unwrap();
    assert!(HistoryStore::initialize_root(old.path()).is_err());
    assert!(HistoryStore::open(old.path()).await.is_err());
    let catalog = old.path().join("must-not-create.redb");
    assert!(HistoryCoordinator::open(&catalog, old.path(), "fixture-owner").await.is_err());
    assert!(!catalog.exists());
    assert_eq!(std::fs::read(&marker).unwrap(), b"openclank-history\nformat=1\n");
    assert_eq!(std::fs::read(&sentinel).unwrap(), b"preserve old data");
    assert_eq!(std::fs::read_dir(old.path().join("immutable")).unwrap().count(), 1);

    let corrupt = tempfile::tempdir().unwrap();
    std::fs::write(corrupt.path().join(".openclank-history-format"), b"openclank-history\nformat=2\nlore-source=f00dbbf07cb449103e211ccbecaa995630f47d2d\npatchset=openclank-v012-p04-1\n").unwrap();
    std::fs::create_dir_all(corrupt.path().join("immutable/pack")).unwrap();
    let corrupt_catalog = corrupt.path().join("must-not-create.redb");
    assert!(HistoryCoordinator::open(&corrupt_catalog, corrupt.path(), "fixture-owner").await.is_err());
    assert!(!corrupt_catalog.exists());

    let fresh = tempfile::tempdir().unwrap();
    let store = HistoryStore::open(fresh.path()).await.unwrap();
    let first = Partition::from([1; 16]);
    let other = Partition::from([2; 16]);
    let address = store.write(first, Context::from([3; 16]), Bytes::from_static(b"durable fixture")).await.unwrap();
    store.flush_checked().await.unwrap();
    assert!(store.read(other, address).await.is_err());
    drop(store);
    let reopened = HistoryStore::open(fresh.path()).await.unwrap();
    assert_eq!(reopened.read(first, address).await.unwrap(), Bytes::from_static(b"durable fixture"));
}
