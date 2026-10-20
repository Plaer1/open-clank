use std::path::PathBuf;
use std::sync::Arc;

use fm_core::config::FmConfig;
use fm_core::embed::NoopEmbeddingClient;
use fm_core::provider::native::NativeProvider;
use fm_core::provider::MemoryProvider;
use fm_core::record::*;
use fm_core::retrieval::collapse::{attest, verify_attestation, CollapsedCandidate};
use fm_core::retrieval::rrf::rrf_merge;
use fm_core::store::sqlite::{
    ForgetOperationState, ForgetSelector, RetentionOperationState, RetentionPolicy, SqliteStore,
};
use fm_core::store::MemoryStore;

fn setup() -> (NativeProvider, Arc<SqliteStore>) {
    let config = FmConfig::default();
    let store = Arc::new(SqliteStore::memory(4).unwrap());
    let embed = Arc::new(NoopEmbeddingClient::new(4));
    let provider = NativeProvider::new(store.clone(), embed, config);
    (provider, store)
}

struct TempDatabase {
    path: PathBuf,
}

impl TempDatabase {
    fn new(label: &str) -> Self {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        Self {
            path: std::env::temp_dir()
                .join(format!("fm-{label}-{}-{nonce}.db", std::process::id())),
        }
    }

    fn as_str(&self) -> &str {
        self.path.to_str().unwrap()
    }
}

impl Drop for TempDatabase {
    fn drop(&mut self) {
        let path = self.path.to_string_lossy();
        for suffix in ["", "-wal", "-shm"] {
            let _ = std::fs::remove_file(format!("{path}{suffix}"));
        }
    }
}

fn setup_file(database: &TempDatabase) -> (NativeProvider, Arc<SqliteStore>) {
    let config = FmConfig::default();
    let store = Arc::new(SqliteStore::new(database.as_str(), 4).unwrap());
    let embed = Arc::new(NoopEmbeddingClient::new(4));
    let provider = NativeProvider::new(store.clone(), embed, config);
    (provider, store)
}

#[test]
fn v2_upgrade_of_copied_database_is_additive_when_fixture_is_requested() {
    // Real-data migration gate, skipped by default so the suite stays
    // hermetic. To run it locally against a copy of a live database (never
    // commit fixture data — privacy):
    //
    //   FM_V2_MIGRATION_FIXTURE=/path/to/copy.db cargo test -p fm-core \
    //       --test integration v2_upgrade_of_copied_database
    //
    // Optionally set FM_V2_MIGRATION_OUTPUT to keep the upgraded copy at a
    // chosen path instead of a temp file.
    let Some(source) = std::env::var_os("FM_V2_MIGRATION_FIXTURE") else {
        // The normal test suite must remain hermetic; Slice 00 supplies the
        // copied-real-data path when running this process-level gate.
        return;
    };
    let output = std::env::var_os("FM_V2_MIGRATION_OUTPUT");
    let database = TempDatabase::new("v2-upgrade");
    let path = output
        .as_ref()
        .map(PathBuf::from)
        .unwrap_or_else(|| database.path.clone());
    std::fs::copy(source, &path).unwrap();
    let store = SqliteStore::new(path.to_str().unwrap(), 4).unwrap();
    let (_database_id, schema_version) = store.database_identity().unwrap();
    // The fixture copy must be migrated all the way to the crate's current
    // schema, not to a hard-coded historical version.
    assert_eq!(schema_version, fm_core::store::sqlite::SCHEMA_VERSION);
    let quality = store.quality_status().unwrap();
    assert!(quality["raw"].as_u64().is_some());
    assert!(quality["curated"].as_u64().is_some());
}

fn scoped_turn(
    owner: &str,
    workspace_id: &str,
    content: &str,
    capture_mode: &str,
    event_id: &str,
    source_message_id: &str,
) -> CompletedTurn {
    CompletedTurn {
        user_text: content.into(),
        assistant_text: String::new(),
        session_key: "integration-session".into(),
        session_id: "integration-session".into(),
        workspace_id: workspace_id.into(),
        workspace_path: None,
        source: "integration".into(),
        owner: Some(owner.into()),
        category: Some("fact".into()),
        metadata: serde_json::json!({
            "capture_mode": capture_mode,
            "source_event_id": event_id,
            "source_message_ids": [source_message_id],
        }),
    }
}

#[tokio::test]
async fn exit_1_dual_record() {
    let (provider, store) = setup();

    let turn = CompletedTurn {
        user_text: "What is Rust?".into(),
        assistant_text: "Rust is a systems programming language.".into(),
        session_key: "test".into(),
        session_id: "s1".into(),
        workspace_id: "global".into(),
        workspace_path: None,
        source: "test".into(),
        owner: Some("alice".into()),
        category: None,
        metadata: Default::default(),
    };

    let result = provider.capture(&turn).await;
    assert!(
        result.records_captured >= 2,
        "should capture user + assistant + curated"
    );
    assert!(result.providers_succeeded >= 1);

    // Verify records exist in store
    let curated = store
        .search_curated_fts_scoped("Rust programming", 10, Some("alice"), Some("global"))
        .await;
    assert!(!curated.is_empty(), "curated store should have records");
}

#[tokio::test]
async fn exit_2_recall_toggle() {
    let (provider, _) = setup();

    let turn = CompletedTurn {
        user_text: "Rust has a borrow checker".into(),
        assistant_text: "Yes, the borrow checker ensures memory safety.".into(),
        session_key: "test".into(),
        session_id: "s1".into(),
        workspace_id: "global".into(),
        workspace_path: None,
        source: "test".into(),
        owner: Some("alice".into()),
        category: None,
        metadata: Default::default(),
    };
    provider.capture(&turn).await;

    let mut q = RecallQuery {
        query: "borrow checker".into(),
        owner: Some("alice".into()),
        workspace_id: Some("global".into()),
        top_k: 5,
        ..Default::default()
    };

    q.mode = RecallMode::LayerA;
    let result_a = provider.recall(&q).await;

    q.mode = RecallMode::LayerB;
    let result_b = provider.recall(&q).await;

    assert!(
        !result_a.memories.is_empty(),
        "LayerA should return results"
    );
    assert!(
        !result_b.memories.is_empty(),
        "LayerB should return results"
    );
    // Native provider uses "hybrid" strategy; layers use "layer_a_hybrid"/"layer_b_hybrid"
    assert_eq!(result_a.recall_strategy, "hybrid");
    assert_eq!(result_b.recall_strategy, "hybrid");
}

#[tokio::test]
async fn exit_3_ground_truth_in_both() {
    let (provider, store) = setup();

    // Capture a high-trust record
    let mut record = MemoryRecord::new("User prefers dark mode");
    record.source_type = SourceType::Human;
    record.trust_score = 0.9;
    record.workspace_id = "global".into();
    record.owner = Some("alice".into());
    let emb = vec![1.0, 0.0, 0.0, 0.0];
    store.upsert_curated(&record, Some(&emb)).await;

    // Capture a low-trust record
    let mut record2 = MemoryRecord::new("Maybe user likes light themes sometimes");
    record2.source_type = SourceType::AutoExtracted;
    record2.trust_score = 0.2;
    record2.workspace_id = "global".into();
    record2.owner = Some("alice".into());
    store.upsert_curated(&record2, None).await;

    let q = RecallQuery {
        query: "dark mode theme".into(),
        owner: Some("alice".into()),
        workspace_id: Some("global".into()),
        top_k: 5,
        ..Default::default()
    };

    let result = provider.recall(&q).await;
    assert!(
        result.ground_truth_preamble.is_some(),
        "GT preamble should be present"
    );
    assert!(
        result
            .ground_truth_preamble
            .unwrap()
            .contains("Ground-Truth"),
        "GT preamble should mention Ground-Truth"
    );
}

#[tokio::test]
async fn exit_4_hybrid_pipeline_deterministic() {
    // RRF is deterministic
    let a = vec![
        make_scored("1", 0.9, "content a"),
        make_scored("2", 0.7, "content b"),
    ];
    let b = vec![
        make_scored("2", 0.95, "content b"),
        make_scored("3", 0.6, "content c"),
    ];

    let r1 = rrf_merge(a.clone(), b.clone(), 60.0);
    let r2 = rrf_merge(a, b, 60.0);
    assert_eq!(r1.len(), r2.len());
    for (a, b) in r1.iter().zip(r2.iter()) {
        assert_eq!(a.record.id, b.record.id);
        assert!((a.score - b.score).abs() < 0.0001);
    }

    // Attestation: attest produces a valid attestation, verify checks it
    let candidates: Vec<CollapsedCandidate> = vec![
        make_scored("1", 0.9, "content a"),
        make_scored("2", 0.8, "content b"),
    ]
    .into_iter()
    .map(|r| CollapsedCandidate {
        record: r,
        salience: 0.5,
        corroboration: 0,
    })
    .collect();

    let att = attest(&candidates);
    assert!(
        verify_attestation(&candidates, &att),
        "verify should pass for same candidates"
    );

    // Tamper: changing a candidate should break verification
    let mut tampered = candidates.clone();
    tampered[0].record.record.id = "tampered".to_string();
    assert!(
        !verify_attestation(&tampered, &att),
        "verify should fail for tampered candidates"
    );
}

#[tokio::test]
async fn exit_5_two_tier() {
    let (provider, _store) = setup();

    // Capture generates raw turns
    let turn = CompletedTurn {
        user_text: "Hello".into(),
        assistant_text: "Hi there!".into(),
        session_key: "test".into(),
        session_id: "s1".into(),
        workspace_id: "global".into(),
        workspace_path: None,
        source: "test".into(),
        owner: Some("alice".into()),
        category: None,
        metadata: Default::default(),
    };
    provider.capture(&turn).await;

    // Search curated
    let curated_results = provider
        .search(&SearchParams {
            query: "Hello".into(),
            kind: None,
            scene: None,
            tier: Tier::Curated,
            limit: 10,
            workspace_id: Some("global".into()),
            owner: Some("alice".into()),
        })
        .await;

    // Search raw
    let raw_results = provider
        .search(&SearchParams {
            query: "Hello".into(),
            kind: None,
            scene: None,
            tier: Tier::Raw,
            limit: 10,
            workspace_id: Some("global".into()),
            owner: Some("alice".into()),
        })
        .await;

    assert!(
        !curated_results.results.is_empty() || !raw_results.results.is_empty(),
        "at least one tier should return results"
    );
}

#[tokio::test]
async fn exit_8_workspace_scoped_recall() {
    let (provider, _store) = setup();

    // Capture in workspace A
    let turn_a = CompletedTurn {
        user_text: "Project alpha uses Rust".into(),
        assistant_text: "Alpha is a Rust project.".into(),
        session_key: "test".into(),
        session_id: "s1".into(),
        workspace_id: "alpha".into(),
        workspace_path: None,
        source: "test".into(),
        owner: Some("alice".into()),
        category: None,
        metadata: Default::default(),
    };
    provider.capture(&turn_a).await;

    // Capture in workspace B
    let turn_b = CompletedTurn {
        user_text: "Project beta uses Python".into(),
        assistant_text: "Beta is a Python project.".into(),
        session_key: "test".into(),
        session_id: "s2".into(),
        workspace_id: "beta".into(),
        workspace_path: None,
        source: "test".into(),
        owner: Some("alice".into()),
        category: None,
        metadata: Default::default(),
    };
    provider.capture(&turn_b).await;

    // Recall from workspace A
    let q_a = RecallQuery {
        query: "project language".into(),
        workspace_id: Some("alpha".into()),
        owner: Some("alice".into()),
        top_k: 10,
        ..Default::default()
    };
    let result_a = provider.recall(&q_a).await;

    // Recall from workspace B
    let q_b = RecallQuery {
        query: "project language".into(),
        workspace_id: Some("beta".into()),
        owner: Some("alice".into()),
        top_k: 10,
        ..Default::default()
    };
    let result_b = provider.recall(&q_b).await;

    // Recall from global: active project scopes never widen to sibling projects.
    let q_global = RecallQuery {
        query: "project language".into(),
        workspace_id: Some("global".into()),
        owner: Some("alice".into()),
        top_k: 10,
        ..Default::default()
    };
    let result_global = provider.recall(&q_global).await;

    // Workspace A should surface alpha content (boosted)
    // Workspace B should surface beta content (boosted)
    // Global should not see either project without an explicit privileged widen.
    assert!(
        !result_a.memories.is_empty(),
        "workspace A should have results"
    );
    assert!(
        !result_b.memories.is_empty(),
        "workspace B should have results"
    );
    assert!(
        result_global.memories.is_empty(),
        "global must not widen to project scopes"
    );
}

#[tokio::test]
async fn exit_9_standalone_no_external_services() {
    // This test passes if cargo test passes at all —
    // no Qdrant, Redis, or external services needed.
    let (provider, _) = setup();

    let turn = CompletedTurn {
        user_text: "Standalone test".into(),
        assistant_text: "Works without external services.".into(),
        session_key: "test".into(),
        session_id: "s1".into(),
        workspace_id: "global".into(),
        workspace_path: None,
        source: "test".into(),
        owner: Some("alice".into()),
        category: None,
        metadata: Default::default(),
    };

    let result = provider.capture(&turn).await;
    assert!(result.records_captured > 0);

    let recall = provider
        .recall(&RecallQuery {
            query: "standalone".into(),
            owner: Some("alice".into()),
            workspace_id: Some("global".into()),
            top_k: 5,
            ..Default::default()
        })
        .await;
    // Should not panic, may return empty
    assert!(recall.memories.len() <= 5);
}

#[tokio::test]
async fn file_backed_candidate_review_persists_across_reopen_and_owner_scope() {
    let database = TempDatabase::new("candidate-review-reopen");
    let candidate_id = {
        let (provider, store) = setup_file(&database);
        provider
            .capture(&scoped_turn(
                "alice",
                "ws-a",
                "Alice keeps the amber observatory ledger.",
                "review_only",
                "candidate-review",
                "candidate-review-message",
            ))
            .await;
        let pending = store
            .list_candidates(Some("alice"), Some("ws-a"), Some("pending"), 10)
            .unwrap();
        assert_eq!(pending.len(), 1);
        pending[0].id.clone()
    };

    let curated_id = {
        let (provider, store) = setup_file(&database);
        assert_eq!(
            store
                .list_candidates(Some("alice"), Some("ws-a"), Some("pending"), 10)
                .unwrap()
                .len(),
            1
        );
        assert!(provider
            .review_candidate(
                &candidate_id,
                true,
                "foreign review must fail",
                "bob",
                "ws-a",
            )
            .await
            .is_err());
        assert_eq!(
            store
                .list_candidates(Some("alice"), Some("ws-a"), Some("pending"), 10)
                .unwrap()
                .len(),
            1
        );
        provider
            .review_candidate(
                &candidate_id,
                true,
                "approved after reopen",
                "alice",
                "ws-a",
            )
            .await
            .unwrap()
            .unwrap()
    };

    let (_provider, store) = setup_file(&database);
    let accepted = store
        .list_candidates(Some("alice"), Some("ws-a"), Some("accepted"), 10)
        .unwrap();
    assert_eq!(accepted.len(), 1);
    assert_eq!(
        accepted[0].accepted_curated_id.as_deref(),
        Some(curated_id.as_str())
    );
    assert!(store
        .get_curated_record(&curated_id, "alice", "ws-a")
        .unwrap()
        .is_some());
    assert!(store
        .get_curated_record(&curated_id, "bob", "ws-a")
        .unwrap()
        .is_none());
    let scope = fm_core::graph::GraphScope::new("alice", "ws-a").unwrap();
    assert!(store
        .graph_fetch(&scope, &scope.node_id("memory", &curated_id))
        .unwrap()
        .is_some());
}

#[tokio::test]
async fn file_backed_forget_commit_and_restore_survive_reopen() {
    let database = TempDatabase::new("forget-restore-reopen");
    {
        let (provider, store) = setup_file(&database);
        store
            .set_retention_policy(
                "alice",
                "ws-a",
                &RetentionPolicy {
                    recovery_seconds: 60,
                    ..RetentionPolicy::default()
                },
            )
            .unwrap();
        provider
            .capture(&scoped_turn(
                "alice",
                "ws-a",
                "Alice keeps the amber launch ledger.",
                "manual",
                "forget-alice",
                "shared-source-message",
            ))
            .await;
        provider
            .capture(&scoped_turn(
                "bob",
                "ws-a",
                "Bob keeps the cobalt launch ledger.",
                "manual",
                "forget-bob",
                "shared-source-message",
            ))
            .await;
    }

    let selector = ForgetSelector::SourceMessageId("shared-source-message".into());
    let operation_id = "0123456789abcdef0123456789abcdef";
    let (preview, committed) = {
        let (_provider, store) = setup_file(&database);
        let preview = store
            .preview_forget("alice", "ws-a", selector.clone())
            .unwrap();
        assert!(!preview.closure.raw_ids.is_empty());
        assert!(!preview.closure.candidate_ids.is_empty());
        assert!(!preview.closure.curated_ids.is_empty());
        assert!(!preview.closure.graph_node_ids.is_empty());
        let committed = store
            .commit_forget_with_operation(
                "alice",
                "ws-a",
                selector.clone(),
                &preview.token,
                operation_id,
            )
            .unwrap();
        assert_eq!(committed.closure, preview.closure);
        assert!(store
            .search_curated_fts_scoped("amber launch", 10, Some("alice"), Some("ws-a"))
            .await
            .is_empty());
        assert!(!store
            .search_curated_fts_scoped("cobalt launch", 10, Some("bob"), Some("ws-a"))
            .await
            .is_empty());
        (preview, committed)
    };

    {
        let (_provider, store) = setup_file(&database);
        let status = store
            .forget_operation_status("alice", "ws-a", operation_id)
            .unwrap();
        assert_eq!(status.state, ForgetOperationState::Committed);
        assert_eq!(status.closure.as_ref(), Some(&preview.closure));
        assert_eq!(
            store
                .commit_forget_with_operation(
                    "alice",
                    "ws-a",
                    selector,
                    &preview.token,
                    operation_id,
                )
                .unwrap(),
            committed
        );
        assert!(store
            .restore_forget("bob", "ws-a", &committed.tombstone_id)
            .is_err());
        assert_eq!(
            store
                .restore_forget("alice", "ws-a", &committed.tombstone_id)
                .unwrap(),
            preview.closure
        );
    }

    let (_provider, store) = setup_file(&database);
    assert_eq!(
        store
            .forget_operation_status("alice", "ws-a", operation_id)
            .unwrap()
            .state,
        ForgetOperationState::Restored
    );
    assert!(!store
        .search_raw_fts_scoped("amber launch", 10, Some("alice"), Some("ws-a"))
        .await
        .is_empty());
    assert!(!store
        .search_curated_fts_scoped("amber launch", 10, Some("alice"), Some("ws-a"))
        .await
        .is_empty());
    for node_id in &preview.closure.graph_node_ids {
        let scope = fm_core::graph::GraphScope::new("alice", "ws-a").unwrap();
        assert!(store.graph_fetch(&scope, node_id).unwrap().is_some());
    }
    assert!(!store
        .search_curated_fts_scoped("cobalt launch", 10, Some("bob"), Some("ws-a"))
        .await
        .is_empty());
}

#[tokio::test]
async fn file_backed_retention_expiry_is_idempotent_and_owner_scoped_after_reopen() {
    let database = TempDatabase::new("retention-reopen");
    let operation_id = "fedcba9876543210fedcba9876543210";
    let preview = {
        let (_provider, store) = setup_file(&database);
        for owner in ["alice", "bob"] {
            store
                .set_retention_policy(
                    owner,
                    "ws-a",
                    &RetentionPolicy {
                        curated_days: Some(1),
                        graph_days: None,
                        ..RetentionPolicy::default()
                    },
                )
                .unwrap();
            let mut record = MemoryRecord::new(format!("{owner} old retention ledger"));
            record.id = format!("{owner}-old-retention");
            record.owner = Some(owner.into());
            record.workspace_id = "ws-a".into();
            record.created_at = "2020-01-01T00:00:00+00:00".into();
            record.updated_at = "2020-01-01T00:00:00+00:00".into();
            assert!(store.upsert_curated(&record, None).await);
        }
        let preview = store.preview_expire_retention("alice", "ws-a").unwrap();
        assert_eq!(
            preview.closure.curated_ids,
            vec!["alice-old-retention".to_string()]
        );
        assert_eq!(
            store
                .expire_retention_with_operation("alice", "ws-a", &preview.token, operation_id,)
                .unwrap(),
            preview.closure
        );
        preview
    };

    let (_provider, store) = setup_file(&database);
    let status = store
        .retention_operation_status("alice", "ws-a", operation_id)
        .unwrap();
    assert_eq!(status.state, RetentionOperationState::Committed);
    assert_eq!(status.closure.as_ref(), Some(&preview.closure));
    assert_eq!(
        store
            .retention_operation_status("bob", "ws-a", operation_id)
            .unwrap()
            .state,
        RetentionOperationState::Absent
    );
    assert_eq!(
        store
            .expire_retention_with_operation("alice", "ws-a", &preview.token, operation_id)
            .unwrap(),
        preview.closure
    );
    assert!(store
        .get_curated_record("alice-old-retention", "alice", "ws-a")
        .unwrap()
        .is_none());
    assert!(store
        .get_curated_record("bob-old-retention", "bob", "ws-a")
        .unwrap()
        .is_some());
}

fn make_scored(id: &str, score: f32, content: &str) -> fm_core::record::ScoredRecord {
    let mut r = MemoryRecord::new(content);
    r.id = id.to_string();
    ScoredRecord {
        record: r,
        score,
        source_label: "test".into(),
    }
}
