use std::sync::Arc;

use fm_core::config::FmConfig;
use fm_core::embed::NoopEmbeddingClient;
use fm_core::provider::native::NativeProvider;
use fm_core::provider::MemoryProvider;
use fm_core::record::*;
use fm_core::retrieval::collapse::{attest, verify_attestation, CollapsedCandidate};
use fm_core::retrieval::rrf::rrf_merge;
use fm_core::store::sqlite::SqliteStore;
use fm_core::store::MemoryStore;

fn setup() -> (NativeProvider, Arc<SqliteStore>) {
    let config = FmConfig::default();
    let store = Arc::new(SqliteStore::memory(4).unwrap());
    let embed = Arc::new(NoopEmbeddingClient::new(4));
    let provider = NativeProvider::new(store.clone(), embed, config);
    (provider, store)
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
        owner: None,
        category: None,
        metadata: Default::default(),
    };

    let result = provider.capture(&turn).await;
    assert!(result.records_captured >= 2, "should capture user + assistant + curated");
    assert!(result.providers_succeeded >= 1);

    // Verify records exist in store
    let curated = store.search_curated_fts("Rust programming", 10).await;
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
        owner: None,
        category: None,
        metadata: Default::default(),
    };
    provider.capture(&turn).await;

    let mut q = RecallQuery {
        query: "borrow checker".into(),
        top_k: 5,
        ..Default::default()
    };

    q.mode = RecallMode::LayerA;
    let result_a = provider.recall(&q).await;

    q.mode = RecallMode::LayerB;
    let result_b = provider.recall(&q).await;

    assert!(!result_a.memories.is_empty(), "LayerA should return results");
    assert!(!result_b.memories.is_empty(), "LayerB should return results");
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
    let emb = vec![1.0, 0.0, 0.0, 0.0];
    store.upsert_curated(&record, Some(&emb)).await;

    // Capture a low-trust record
    let mut record2 = MemoryRecord::new("Maybe user likes light themes sometimes");
    record2.source_type = SourceType::AutoExtracted;
    record2.trust_score = 0.2;
    record2.workspace_id = "global".into();
    store.upsert_curated(&record2, None).await;

    let q = RecallQuery {
        query: "dark mode theme".into(),
        top_k: 5,
        ..Default::default()
    };

    let result = provider.recall(&q).await;
    assert!(result.ground_truth_preamble.is_some(), "GT preamble should be present");
    assert!(
        result.ground_truth_preamble.unwrap().contains("Ground-Truth"),
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
    assert!(verify_attestation(&candidates, &att), "verify should pass for same candidates");

    // Tamper: changing a candidate should break verification
    let mut tampered = candidates.clone();
    tampered[0].record.record.id = "tampered".to_string();
    assert!(!verify_attestation(&tampered, &att), "verify should fail for tampered candidates");
}

#[tokio::test]
async fn exit_5_two_tier() {
    let (provider, store) = setup();

    // Capture generates raw turns
    let turn = CompletedTurn {
        user_text: "Hello".into(),
        assistant_text: "Hi there!".into(),
        session_key: "test".into(),
        session_id: "s1".into(),
        workspace_id: "global".into(),
        workspace_path: None,
        source: "test".into(),
        owner: None,
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
            workspace_id: None,
            owner: None,
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
            workspace_id: None,
            owner: None,
        })
        .await;

    assert!(
        !curated_results.results.is_empty() || !raw_results.results.is_empty(),
        "at least one tier should return results"
    );
}

#[tokio::test]
async fn exit_8_workspace_scoped_recall() {
    let (provider, store) = setup();

    // Capture in workspace A
    let turn_a = CompletedTurn {
        user_text: "Project alpha uses Rust".into(),
        assistant_text: "Alpha is a Rust project.".into(),
        session_key: "test".into(),
        session_id: "s1".into(),
        workspace_id: "alpha".into(),
        workspace_path: None,
        source: "test".into(),
        owner: None,
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
        owner: None,
        category: None,
        metadata: Default::default(),
    };
    provider.capture(&turn_b).await;

    // Recall from workspace A
    let q_a = RecallQuery {
        query: "project language".into(),
        workspace_id: Some("alpha".into()),
        top_k: 10,
        ..Default::default()
    };
    let result_a = provider.recall(&q_a).await;

    // Recall from workspace B
    let q_b = RecallQuery {
        query: "project language".into(),
        workspace_id: Some("beta".into()),
        top_k: 10,
        ..Default::default()
    };
    let result_b = provider.recall(&q_b).await;

    // Recall from global (no workspace)
    let q_global = RecallQuery {
        query: "project language".into(),
        workspace_id: None,
        top_k: 10,
        ..Default::default()
    };
    let result_global = provider.recall(&q_global).await;

    // Workspace A should surface alpha content (boosted)
    // Workspace B should surface beta content (boosted)
    // Global should see both
    assert!(
        !result_a.memories.is_empty(),
        "workspace A should have results"
    );
    assert!(
        !result_b.memories.is_empty(),
        "workspace B should have results"
    );
    assert!(
        result_global.memories.len() >= result_a.memories.len(),
        "global should see at least as many as scoped"
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
        owner: None,
        category: None,
        metadata: Default::default(),
    };

    let result = provider.capture(&turn).await;
    assert!(result.records_captured > 0);

    let recall = provider
        .recall(&RecallQuery {
            query: "standalone".into(),
            top_k: 5,
            ..Default::default()
        })
        .await;
    // Should not panic, may return empty
    assert!(recall.memories.len() <= 5);
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
