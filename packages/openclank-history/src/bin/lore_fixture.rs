use bytes::Bytes;
#[cfg(feature = "qualification-fixtures")]
use lore_storage::abort_compaction_at_phase;
use lore_storage::{Context, Partition};
use openclank_history::HistoryStore;

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let root = std::env::args().nth(2).expect("fixture root");
    let partition = Partition::from([6; 16]);
    match std::env::args().nth(1).as_deref() {
        Some("marker-only") => {
            HistoryStore::initialize_root(&root).unwrap();
        }
        Some("write-abort") => {
            let history = HistoryStore::open(&root).await.unwrap();
            let address = history
                .write(
                    partition,
                    Context::from([4; 16]),
                    Bytes::from_static(b"abort fixture"),
                )
                .await
                .unwrap();
            history.flush_checked().await.unwrap();
            std::fs::write(
                std::path::Path::new(&root).join("fixture.address"),
                address.hash.to_string(),
            )
            .unwrap();
            std::process::abort();
        }
        Some("read") => {
            let text = std::fs::read_to_string(std::path::Path::new(&root).join("fixture.address"))
                .unwrap();
            let hash = text.parse().unwrap();
            let address = lore_storage::Address {
                context: Context::from([4; 16]),
                hash,
            };
            let bytes = HistoryStore::open(&root)
                .await
                .unwrap()
                .read(partition, address)
                .await
                .unwrap();
            assert_eq!(bytes.as_ref(), b"abort fixture");
        }
        Some("prepare-compaction") => {
            let history = HistoryStore::open(&root).await.unwrap();
            let expired = history
                .write(
                    partition,
                    Context::from([1; 16]),
                    Bytes::from_static(b"compaction fixture"),
                )
                .await
                .unwrap();
            let survivor = history
                .write(
                    partition,
                    Context::from([2; 16]),
                    Bytes::from_static(b"compaction fixture"),
                )
                .await
                .unwrap();
            history.flush_checked().await.unwrap();
            history.expire(partition, expired).await.unwrap();
            history.flush_checked().await.unwrap();
            std::fs::write(
                std::path::Path::new(&root).join("compaction.addresses"),
                format!("{}\n{}\n", expired.hash, survivor.hash),
            )
            .unwrap();
        }
        Some("compact-abort") => {
            let history = HistoryStore::open(&root).await.unwrap();
            let phase: u8 = std::env::args().nth(3).unwrap().parse().unwrap();
            abort_compaction_at_phase(phase);
            history.compact_checked().await.unwrap();
        }
        Some("compact") => {
            let history = HistoryStore::open(&root).await.unwrap();
            history.compact_checked().await.unwrap();
        }
        Some("read-compaction") => {
            let history = HistoryStore::open(&root).await.unwrap();
            let lines =
                std::fs::read_to_string(std::path::Path::new(&root).join("compaction.addresses"))
                    .unwrap();
            let mut hashes = lines.lines().map(|line| line.parse().unwrap());
            let expired = lore_storage::Address {
                context: Context::from([1; 16]),
                hash: hashes.next().unwrap(),
            };
            let survivor = lore_storage::Address {
                context: Context::from([2; 16]),
                hash: hashes.next().unwrap(),
            };
            assert!(history.read(partition, expired).await.is_err());
            assert_eq!(
                history.read(partition, survivor).await.unwrap().as_ref(),
                b"compaction fixture"
            );
        }
        _ => panic!("unknown fixture mode"),
    }
}
