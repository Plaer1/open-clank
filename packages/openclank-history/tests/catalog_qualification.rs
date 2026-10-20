use bytes::Bytes;
use openclank_history::catalog::{
    ActionState, BeginResult, Catalog, CatalogConflict, LiveReceipt, LiveStatus, LoreRef,
    ResourceKey, VersionContent, VersionReceipt,
};
use openclank_history::operations::{ActionRequest, HistoryCoordinator, begin, tombstone_version};
#[cfg(feature = "test_faults")]
use openclank_history::operations::{CaptureFault, inject_capture_fault};

static TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn scoped_resource(resource_id: &str) -> ResourceKey {
    ResourceKey {
        account_id: "account".into(),
        workspace_id: "workspace".into(),
        provider: "files".into(),
        resource_id: resource_id.into(),
    }
}

fn request(action: &str, operation: &str, actor: &str) -> ActionRequest {
    ActionRequest {
        schema_version: 1,
        action_id: action.into(),
        actor_account_id: "account".into(),
        resource_key: scoped_resource("resource"),
        physical_lease_keys: vec![],
        guard_resource_ids: vec![],
        modified_resource_ids: vec![],
        operation: operation.into(),
        expected_revision: Some("rev-1".into()),
        actor_id: actor.into(),
        actor_kind: "user".into(),
        session_id: None,
        run_id: None,
        task_id: None,
        tool_id: None,
        before_revision: None,
        expected_after_revision: None,
        original_locator: None,
        destination_locator: None,
        timestamp_millis: None,
        coverage: None,
        per_resource_outcomes: None,
    }
}

#[tokio::test]
async fn durable_partition_and_action_phases_survive_restart() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("catalog.redb");
    let coordinator = HistoryCoordinator::open(&db, dir.path().join("lore"), "account")
        .await
        .unwrap();
    let partition = coordinator.catalog().owner_partition("account").unwrap();
    assert_ne!(partition, [0; 16]);
    let action = match begin(coordinator.catalog(), request("a1", "replace", "actor")).unwrap() {
        BeginResult::New(record) => record,
        _ => panic!("expected new action"),
    };
    let before = coordinator
        .capture_before(
            &action.action_id,
            Some(Bytes::from_static(b"before")),
            "before-fingerprint",
        )
        .await
        .unwrap()
        .before
        .unwrap();
    let competing = match begin(
        coordinator.catalog(),
        request("competing", "replace", "actor"),
    )
    .unwrap()
    {
        BeginResult::New(record) => record,
        _ => unreachable!(),
    };
    assert!(
        coordinator
            .capture_before(
                &competing.action_id,
                Some(Bytes::from_static(b"must-wait")),
                "fp"
            )
            .await
            .is_err()
    );
    assert_eq!(
        coordinator
            .catalog()
            .get_action(&competing.action_id)
            .unwrap()
            .unwrap()
            .state,
        ActionState::Intent
    );
    coordinator.begin_apply(&action.action_id).unwrap();
    coordinator
        .record_live(
            &action.action_id,
            LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::Committed,
                fingerprint: Some("live".into()),
            },
        )
        .unwrap();
    coordinator
        .capture_after(
            &action.action_id,
            Some(Bytes::from_static(b"after")),
            "after-fingerprint",
        )
        .await
        .unwrap();
    coordinator.complete(&action.action_id).unwrap();
    drop(coordinator);
    let reopened = HistoryCoordinator::open(&db, dir.path().join("lore"), "account")
        .await
        .unwrap();
    assert_eq!(
        reopened.catalog().get_action("a1").unwrap().unwrap().state,
        ActionState::Complete
    );
    let reopened_record = reopened.catalog().get_action("a1").unwrap().unwrap();
    assert_eq!(reopened_record.before, Some(before.clone()));
    let after = reopened_record.after.as_ref().unwrap();
    let VersionContent::Bytes(before_ref) = &before.content else {
        panic!("expected byte receipt")
    };
    let VersionContent::Bytes(after_ref) = &after.content else {
        panic!("expected byte receipt")
    };
    assert_ne!(before_ref.context, [0; 16]);
    assert_ne!(after_ref.context, [0; 16]);
    assert_ne!(before_ref.context, after_ref.context);
    assert_eq!(
        reopened
            .read_version("a1", reopened_record.before.as_ref().unwrap())
            .await
            .unwrap()
            .unwrap()
            .as_ref(),
        b"before"
    );
    assert_eq!(
        reopened
            .read_version("a1", after)
            .await
            .unwrap()
            .unwrap()
            .as_ref(),
        b"after"
    );
}

#[test]
fn idempotency_binds_digest_and_actor_and_preserves_tombstones() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(dir.path().join("catalog.redb"), "account").unwrap();
    let record = match begin(&catalog, request("a1", "replace", "actor")).unwrap() {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    assert!(matches!(
        begin(&catalog, request("a1", "replace", "actor")).unwrap(),
        BeginResult::Existing(_)
    ));
    assert!(
        matches!(begin(&catalog, request("a1", "delete", "actor")), Err(e) if e.downcast_ref::<CatalogConflict>() == Some(&CatalogConflict::ActionDigestMismatch))
    );
    assert!(
        matches!(begin(&catalog, request("a1", "replace", "other")), Err(e) if e.downcast_ref::<CatalogConflict>() == Some(&CatalogConflict::ActorMismatch))
    );
    let tombstone = tombstone_version(&record.action_id, "absent");
    assert_eq!(tombstone.content, VersionContent::Tombstone);
    assert_ne!(tombstone.version_id, record.action_id);
}

#[tokio::test]
async fn sorted_leases_have_no_ttl_and_reconcile_never_replays_live_effects() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        dir.path().join("catalog.redb"),
        dir.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let catalog = coordinator.catalog();
    let record = match begin(catalog, request("a1", "replace", "actor")).unwrap() {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    let ids = vec!["a".into(), "b".into()];
    catalog.acquire_leases(&record.action_id, &ids).unwrap();
    assert!(
        matches!(catalog.acquire_leases("other", &["b".into(), "a".into()]), Err(e) if e.downcast_ref::<CatalogConflict>() == Some(&CatalogConflict::LeaseOrder))
    );
    assert!(
        matches!(catalog.acquire_leases("other", &ids), Err(e) if e.downcast_ref::<CatalogConflict>() == Some(&CatalogConflict::LeaseConflict))
    );
    catalog.release_leases(&record.action_id, &ids).unwrap();
    assert!(coordinator.complete(&record.action_id).is_err());
    let reconciled = coordinator
        .reconcile(|action| {
            Ok(LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::Unknown,
                fingerprint: None,
            })
        })
        .unwrap();
    assert_eq!(reconciled[0].state, ActionState::NeedsReconciliation);
    assert!(
        catalog
            .get_action(&record.action_id)
            .unwrap()
            .unwrap()
            .after
            .is_none()
    );
    let unknown = match begin(catalog, request("a2", "replace", "actor")).unwrap() {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    let recovered = coordinator
        .reconcile(|action| {
            if action.action_id == unknown.action_id {
                Err("resolver unavailable".into())
            } else {
                Ok(LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::Unknown,
                    fingerprint: None,
                })
            }
        })
        .unwrap();
    assert!(
        recovered.iter().any(
            |r| r.action_id == unknown.action_id && r.state == ActionState::NeedsReconciliation
        )
    );
}

#[cfg(feature = "test_faults")]
#[tokio::test]
async fn coordinator_faults_leave_durable_phase_markers_without_replaying_live_work() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let coordinator = HistoryCoordinator::open(&db, &lore, "account")
        .await
        .unwrap();
    let before = match begin(
        coordinator.catalog(),
        request("before-fault", "replace", "actor"),
    )
    .unwrap()
    {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    inject_capture_fault(CaptureFault::AfterMarker);
    assert!(
        coordinator
            .capture_before(
                &before.action_id,
                Some(Bytes::from_static(b"never-written")),
                "fp"
            )
            .await
            .is_err()
    );
    assert_eq!(
        coordinator
            .catalog()
            .get_action(&before.action_id)
            .unwrap()
            .unwrap()
            .state,
        ActionState::CaptureFailed
    );

    let after_flush = match begin(
        coordinator.catalog(),
        request("flush-fault", "replace", "actor"),
    )
    .unwrap()
    {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    inject_capture_fault(CaptureFault::AfterFlush);
    assert!(
        coordinator
            .capture_before(
                &after_flush.action_id,
                Some(Bytes::from_static(b"flushed-without-ref")),
                "fp"
            )
            .await
            .is_err()
    );
    assert_eq!(
        coordinator
            .catalog()
            .get_action(&after_flush.action_id)
            .unwrap()
            .unwrap()
            .state,
        ActionState::CaptureFailed
    );
    drop(coordinator);

    let reopened = HistoryCoordinator::open(&db, &lore, "account")
        .await
        .unwrap();
    let recovered = reopened
        .reconcile(|action| {
            Ok(LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::Unknown,
                fingerprint: None,
            })
        })
        .unwrap();
    assert!(recovered
        .iter()
        .any(|r| r.action_id == before.action_id && r.state == ActionState::NeedsReconciliation));
    assert!(recovered.iter().any(
        |r| r.action_id == after_flush.action_id && r.state == ActionState::NeedsReconciliation
    ));
    assert!(
        reopened
            .catalog()
            .get_action(&after_flush.action_id)
            .unwrap()
            .unwrap()
            .before
            .is_none()
    );

    let committed = match begin(
        reopened.catalog(),
        request("after-fault", "replace", "actor"),
    )
    .unwrap()
    {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    reopened
        .capture_before(
            &committed.action_id,
            Some(Bytes::from_static(b"before")),
            "fp",
        )
        .await
        .unwrap();
    reopened.begin_apply(&committed.action_id).unwrap();
    reopened
        .record_live(
            &committed.action_id,
            LiveReceipt { committed_resources: Vec::new(),
                action_id: committed.action_id.clone(),
                status: LiveStatus::Committed,
                fingerprint: Some("live".into()),
            },
        )
        .unwrap();
    inject_capture_fault(CaptureFault::AfterLiveMarker);
    assert!(
        reopened
            .capture_after(
                &committed.action_id,
                Some(Bytes::from_static(b"after")),
                "fp"
            )
            .await
            .is_err()
    );
    assert_eq!(
        reopened
            .catalog()
            .get_action(&committed.action_id)
            .unwrap()
            .unwrap()
            .state,
        ActionState::AfterCaptureFailed
    );
    let receipt = reopened
        .catalog()
        .get_action(&committed.action_id)
        .unwrap()
        .unwrap()
        .receipt();
    assert_eq!(receipt.phase, ActionState::AfterCaptureFailed);
    assert_eq!(receipt.history_status, ActionState::Applied);
    assert_eq!(receipt.live_outcome, Some(LiveStatus::Committed));
    let reused = begin(
        reopened.catalog(),
        request("after-fault-reuse", "replace", "actor"),
    )
    .unwrap();
    assert!(matches!(reused, BeginResult::New(_)));
    let mut resolver_calls = 0;
    let reconciled = reopened
        .reconcile(|action| {
            resolver_calls += 1;
            Ok(LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::Unknown,
                fingerprint: None,
            })
        })
        .unwrap();
    assert!(resolver_calls >= 1);
    assert!(
        reconciled
            .iter()
            .any(|r| r.action_id == committed.action_id
                && r.state == ActionState::NeedsReconciliation)
    );
}

#[tokio::test]
async fn tombstone_empty_bytes_and_read_error_remain_distinct() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        dir.path().join("catalog.redb"),
        dir.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let tomb = match begin(coordinator.catalog(), request("tomb", "delete", "actor")).unwrap() {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    let tomb_record = coordinator
        .capture_before(&tomb.action_id, None, "missing")
        .await
        .unwrap();
    assert!(matches!(
        tomb_record.before.unwrap().content,
        VersionContent::Tombstone
    ));
    let mut empty_request = request("empty", "replace", "actor");
    empty_request.resource_key.resource_id = "resource-empty".into();
    let empty = match begin(coordinator.catalog(), empty_request).unwrap() {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    let empty_record = coordinator
        .capture_before(&empty.action_id, Some(Bytes::new()), "empty")
        .await
        .unwrap();
    let empty_receipt = empty_record.before.unwrap();
    assert_eq!(empty_receipt.content, VersionContent::Empty);
    assert_eq!(
        coordinator
            .read_version(&empty.action_id, &empty_receipt)
            .await
            .unwrap(),
        Some(Bytes::new())
    );
    let malformed = VersionReceipt {
        version_id: "bad".into(),
        content: VersionContent::Bytes(LoreRef {
            context: [1; 16],
            hash_hex: "not-a-hash".into(),
        }),
        fingerprint: "bad".into(),
    };
    assert!(
        coordinator
            .read_version(&empty.action_id, &malformed)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn partial_multi_resource_outcome_releases_union_lease_for_reuse() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        dir.path().join("catalog.redb"),
        dir.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let mut first_request = request("multi-partial", "move", "actor");
    first_request.guard_resource_ids = vec![scoped_resource("source")];
    first_request.modified_resource_ids = vec![scoped_resource("destination")];
    first_request.per_resource_outcomes = Some(vec![
        openclank_history::catalog::ResourceOutcome {
            resource_id: "source".into(),
            status: "committed".into(),
            revision: Some("r2".into()),
        },
        openclank_history::catalog::ResourceOutcome {
            resource_id: "destination".into(),
            status: "failed".into(),
            revision: None,
        },
    ]);
    let first = match begin(coordinator.catalog(), first_request).unwrap() {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    coordinator
        .capture_before(&first.action_id, Some(Bytes::from_static(b"before")), "fp")
        .await
        .unwrap();
    coordinator.begin_apply(&first.action_id).unwrap();
    let partial = coordinator
        .record_live(
            &first.action_id,
            LiveReceipt { committed_resources: Vec::new(),
                action_id: first.action_id.clone(),
                status: LiveStatus::Partial,
                fingerprint: Some("partial".into()),
            },
        )
        .unwrap();
    assert_eq!(partial.state, ActionState::Partial);
    let mut next_request = request("multi-next", "move", "actor");
    next_request.guard_resource_ids = vec![scoped_resource("source")];
    next_request.modified_resource_ids = vec![scoped_resource("destination")];
    let next = match begin(coordinator.catalog(), next_request).unwrap() {
        BeginResult::New(r) => r,
        _ => unreachable!(),
    };
    assert!(
        coordinator
            .capture_before(&next.action_id, Some(Bytes::from_static(b"retry")), "retry")
            .await
            .is_ok()
    );
}

#[test]
fn scoped_resource_ids_do_not_conflict_across_tenants_or_providers() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let catalog = Catalog::open(dir.path().join("catalog.redb"), "account").unwrap();
    let variants = [
        scoped_resource("same-id"),
        ResourceKey {
            account_id: "other-account".into(),
            ..scoped_resource("same-id")
        },
        ResourceKey {
            workspace_id: "other-workspace".into(),
            ..scoped_resource("same-id")
        },
        ResourceKey {
            provider: "notes".into(),
            ..scoped_resource("same-id")
        },
    ];
    for (index, resource) in variants.iter().enumerate() {
        catalog
            .acquire_leases(&format!("action-{index}"), &[resource.scoped_lease_id()])
            .unwrap();
    }
}

#[tokio::test]
async fn server_resolved_physical_aliases_conflict_for_distinct_typed_resources() {
    let _lock = TEST_LOCK.lock().unwrap();
    let dir = tempfile::tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        dir.path().join("catalog.redb"),
        dir.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let mut first_request = request("physical-first", "replace", "actor");
    first_request.resource_key = scoped_resource("resource-a");
    first_request.physical_lease_keys = vec!["volume:inode:42".into()];
    let first = match begin(coordinator.catalog(), first_request).unwrap() {
        BeginResult::New(record) => record,
        _ => unreachable!(),
    };
    coordinator
        .capture_before(
            &first.action_id,
            Some(Bytes::from_static(b"first")),
            "first",
        )
        .await
        .unwrap();

    let mut second_request = request("physical-second", "replace", "actor");
    second_request.resource_key = scoped_resource("resource-b");
    second_request.physical_lease_keys = vec!["volume:inode:42".into()];
    let second = match begin(coordinator.catalog(), second_request).unwrap() {
        BeginResult::New(record) => record,
        _ => unreachable!(),
    };
    assert!(
        coordinator
            .capture_before(
                &second.action_id,
                Some(Bytes::from_static(b"second")),
                "second"
            )
            .await
            .is_err()
    );
}
