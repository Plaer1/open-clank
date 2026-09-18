use bytes::Bytes;
use openclank_history::capture::{CaptureAdapter, CaptureEnvelope, CoverageKind, CoverageReceipt};
use openclank_history::catalog::{ActionState, LiveReceipt, LiveStatus, Locator, ResourceKey};
use openclank_history::operations::HistoryCoordinator;
use tempfile::tempdir;

fn envelope(action_id: &str, actor_kind: &str) -> CaptureEnvelope {
    CaptureEnvelope {
        action_id: action_id.into(),
        actor_id: "actor".into(),
        actor_account_id: "account".into(),
        actor_kind: actor_kind.into(),
        session_id: Some("session".into()),
        run_id: Some("run".into()),
        task_id: None,
        tool_id: Some("tool".into()),
        resource_key: ResourceKey {
            account_id: "account".into(),
            workspace_id: "workspace".into(),
            provider: "copal".into(),
            resource_id: "doc".into(),
        },
        guard_resource_ids: Vec::new(),
        modified_resource_ids: Vec::new(),
        operation: "replace".into(),
        expected_revision: None,
        before_revision: None,
        expected_after_revision: None,
        original_locator: Some(Locator::from("notes/doc")),
        destination_locator: None,
        per_resource_outcomes: None,
        coverage: CoverageReceipt {
            kind: CoverageKind::KnownMutationHooks,
            captured_at_millis: 7,
            roots: vec![Locator::from("notes")],
            exclusions: vec!["attachments".into()],
        },
    }
}

#[tokio::test]
async fn user_and_agent_envelopes_capture_once_and_keep_provenance() {
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let adapter = CaptureAdapter::new(&coordinator);
    let first = adapter
        .prepare(
            envelope("action-1", "user"),
            Some(Bytes::from_static(b"before")),
            "rev-before",
        )
        .await
        .unwrap();
    assert_eq!(first.state, ActionState::BeforeDurable);
    assert_eq!(first.actor_kind, "user");
    assert_eq!(
        first.coverage.as_ref().unwrap().metadata.as_ref().unwrap()["coverage_kind"],
        "KnownMutationHooks"
    );
    let duplicate = adapter
        .prepare(
            envelope("action-1", "user"),
            Some(Bytes::from_static(b"duplicate")),
            "rev-before",
        )
        .await
        .unwrap();
    assert_eq!(duplicate.action_id, first.action_id);
    let finished = adapter
        .finish(
            "action-1",
            LiveReceipt {
                action_id: "action-1".into(),
                status: LiveStatus::Committed,
                fingerprint: Some("rev-after".into()),
            },
            Some(Bytes::from_static(b"after")),
            "rev-after",
        )
        .await
        .unwrap();
    assert_eq!(finished.state, ActionState::Complete);
    assert_eq!(finished.live.unwrap().status, LiveStatus::Committed);
    coordinator.shutdown_checked().await.unwrap();
}
