use bytes::Bytes;
use openclank_history::capture::{CaptureAdapter, CaptureEnvelope, CoverageKind, CoverageReceipt};
use openclank_history::catalog::{
    ActionState, CaptureManifest, Locator, ResourceExistence, ResourceKey, ResourceMetadata,
    ResourceType,
};
use openclank_history::operations::{BatchCaptureInput, HistoryCoordinator};
use openclank_history::protocol::{
    AuthContext, BatchPrepareEntry, RequestEnvelope, ServiceRequest,
};
use tempfile::tempdir;

fn key(resource_id: &str) -> ResourceKey {
    ResourceKey {
        account_id: "account".into(),
        workspace_id: "workspace".into(),
        provider: "files".into(),
        resource_id: resource_id.into(),
    }
}

fn envelope() -> CaptureEnvelope {
    CaptureEnvelope {
        action_id: "batch-1".into(),
        actor_id: "actor".into(),
        actor_account_id: "account".into(),
        actor_kind: "agent".into(),
        session_id: Some("session".into()),
        run_id: Some("run".into()),
        task_id: None,
        tool_id: Some("filesystem.write".into()),
        resource_key: key("source"),
        guard_resource_ids: Vec::new(),
        modified_resource_ids: vec![key("destination"), key("created")],
        operation: "rename-overwrite".into(),
        expected_revision: None,
        before_revision: None,
        expected_after_revision: None,
        original_locator: Some(Locator::from("src/a.txt")),
        destination_locator: Some(Locator::from("dst/a.txt")),
        per_resource_outcomes: None,
        coverage: CoverageReceipt {
            kind: CoverageKind::KnownMutationHooks,
            captured_at_millis: 1,
            roots: vec![Locator::from("workspace")],
            exclusions: Vec::new(),
        },
    }
}

fn entry(
    resource_id: &str,
    old: &str,
    new: &str,
    existence: ResourceExistence,
    content: Option<&[u8]>,
) -> BatchCaptureInput {
    BatchCaptureInput {
        resource_key: key(resource_id),
        old_locator: Some(Locator::from(old)),
        new_locator: Some(Locator::from(new)),
        expected_revision: Some(format!("revision:{resource_id}").into()),
        existence,
        resource_type: ResourceType::File,
        metadata: ResourceMetadata {
            mode: Some(0o640),
            size: content.map(|bytes| bytes.len() as u64),
            modified_millis: Some(7),
            opaque: None,
        },
        content: content.map(Bytes::copy_from_slice),
        fingerprint: format!("fingerprint:{resource_id}"),
        coverage: CaptureManifest::default(),
    }
}

#[tokio::test]
async fn batch_prepare_persists_exact_source_destination_and_absent_preimages() {
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let adapter = CaptureAdapter::new(&coordinator);
    let record = adapter
        .prepare_batch(
            envelope(),
            vec![
                entry(
                    "source",
                    "src/a.txt",
                    "dst/a.txt",
                    ResourceExistence::Present,
                    Some(b"source-before"),
                ),
                entry(
                    "destination",
                    "dst/a.txt",
                    "dst/a.txt",
                    ResourceExistence::Present,
                    Some(b"destination-before"),
                ),
                entry(
                    "created",
                    "dst/new.txt",
                    "dst/new.txt",
                    ResourceExistence::Absent,
                    None,
                ),
            ],
        )
        .await
        .unwrap();

    assert_eq!(record.state, ActionState::BeforeDurable);
    let batch = record.mutation_batch.as_ref().unwrap();
    assert!(batch.prepare.acknowledged);
    assert_eq!(batch.prepare.resource_count, 3);
    assert_eq!(record.before_resources.len(), 3);
    assert_eq!(
        record.before_resources[0]
            .old_locator
            .as_ref()
            .unwrap()
            .location_label,
        "src/a.txt"
    );
    assert_eq!(
        record.before_resources[1]
            .old_locator
            .as_ref()
            .unwrap()
            .location_label,
        "dst/a.txt"
    );
    assert_eq!(
        record.before_resources[2].existence,
        ResourceExistence::Absent
    );
    assert_ne!(
        record.before_resources[0].before,
        record.before_resources[1].before
    );

    for (resource, expected) in record.before_resources.iter().zip([
        Some(Bytes::from_static(b"source-before")),
        Some(Bytes::from_static(b"destination-before")),
        None,
    ]) {
        let restored = coordinator
            .read_version("batch-1", resource.before.as_ref().unwrap())
            .await
            .unwrap();
        assert_eq!(restored, expected);
    }
    coordinator.abort("batch-1").unwrap();
    coordinator.shutdown_checked().await.unwrap();
}

#[tokio::test]
async fn batch_prepare_rejects_foreign_scope_and_inexact_directory_before_lore_write() {
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let adapter = CaptureAdapter::new(&coordinator);

    let mut foreign = entry(
        "foreign",
        "foreign.txt",
        "foreign.txt",
        ResourceExistence::Present,
        Some(b"must-not-capture"),
    );
    foreign.resource_key.account_id = "other-account".into();
    let error = adapter
        .prepare_batch(envelope(), vec![foreign])
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("scope_conflict"));
    assert_eq!(
        coordinator
            .catalog()
            .get_action("batch-1")
            .unwrap()
            .unwrap()
            .state,
        ActionState::Intent
    );

    let mut directory = entry(
        "directory",
        "folder",
        "folder",
        ResourceExistence::Present,
        None,
    );
    directory.resource_type = ResourceType::Directory;
    let mut directory_envelope = envelope();
    directory_envelope.action_id = "batch-dir".into();
    directory_envelope.modified_resource_ids = vec![directory.resource_key.clone()];
    let error = adapter
        .prepare_batch(directory_envelope, vec![directory])
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("inexact_preimage"));
    assert_eq!(
        coordinator
            .catalog()
            .get_action("batch-dir")
            .unwrap()
            .unwrap()
            .state,
        ActionState::Intent
    );
    coordinator.abort("batch-1").unwrap();
    coordinator.abort("batch-dir").unwrap();
    coordinator.shutdown_checked().await.unwrap();
}

#[tokio::test]
async fn batch_prepare_requires_every_declared_resource_and_ignores_forged_aliases() {
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let adapter = CaptureAdapter::new(&coordinator);

    let mut omitted_envelope = envelope();
    omitted_envelope.action_id = "batch-omitted".into();
    let error = adapter
        .prepare_batch(
            omitted_envelope,
            vec![entry(
                "source",
                "src/a.txt",
                "src/a.txt",
                ResourceExistence::Present,
                Some(b"source-before"),
            )],
        )
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("missing_resource"));

    let mut first_envelope = envelope();
    first_envelope.action_id = "batch-alias-a".into();
    first_envelope.resource_key = key("alias-a");
    first_envelope.modified_resource_ids.clear();
    let mut first = entry(
        "alias-a",
        "a.txt",
        "a.txt",
        ResourceExistence::Present,
        Some(b"a-before"),
    );
    first.old_locator.as_mut().unwrap().opaque_ref = Some("forged-shared-alias".into());
    first.new_locator.as_mut().unwrap().opaque_ref = Some("forged-shared-alias".into());
    let first_record = adapter
        .prepare_batch(first_envelope, vec![first])
        .await
        .unwrap();
    assert!(first_record.physical_lease_keys.is_empty());

    let mut second_envelope = envelope();
    second_envelope.action_id = "batch-alias-b".into();
    second_envelope.resource_key = key("alias-b");
    second_envelope.modified_resource_ids.clear();
    let mut second = entry(
        "alias-b",
        "b.txt",
        "b.txt",
        ResourceExistence::Present,
        Some(b"b-before"),
    );
    second.old_locator.as_mut().unwrap().opaque_ref = Some("forged-shared-alias".into());
    second.new_locator.as_mut().unwrap().opaque_ref = Some("forged-shared-alias".into());
    adapter
        .prepare_batch(second_envelope, vec![second])
        .await
        .expect("a client locator alias must not block another logical resource");

    coordinator.abort("batch-omitted").unwrap();
    coordinator.abort("batch-alias-a").unwrap();
    coordinator.abort("batch-alias-b").unwrap();
    coordinator.shutdown_checked().await.unwrap();
}

#[tokio::test]
async fn batch_prepare_rejects_malformed_wire_preimages() {
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let adapter = CaptureAdapter::new(&coordinator);

    let absent_with_content = entry(
        "absent-content",
        "new.txt",
        "new.txt",
        ResourceExistence::Absent,
        Some(b"forged-before"),
    );
    let mut absent_envelope = envelope();
    absent_envelope.action_id = "batch-absent-content".into();
    absent_envelope.resource_key = absent_with_content.resource_key.clone();
    let error = adapter
        .prepare_batch(absent_envelope, vec![absent_with_content.clone()])
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("invalid_preimage"));

    let present_without_content = entry(
        "present-missing",
        "existing.txt",
        "existing.txt",
        ResourceExistence::Present,
        None,
    );
    let mut present_envelope = envelope();
    present_envelope.action_id = "batch-present-missing".into();
    present_envelope.resource_key = present_without_content.resource_key.clone();
    let error = adapter
        .prepare_batch(present_envelope, vec![present_without_content])
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("missing_preimage"));

    let mut directory = entry(
        "directory-forged",
        "folder",
        "folder",
        ResourceExistence::Present,
        Some(b"caller-claimed-manifest"),
    );
    directory.resource_type = ResourceType::Directory;
    directory.coverage.metadata = Some(serde_json::json!({ "exact_preimage": true }));
    let mut directory_envelope = envelope();
    directory_envelope.action_id = "batch-directory-forged".into();
    directory_envelope.resource_key = directory.resource_key.clone();
    let error = adapter
        .prepare_batch(directory_envelope, vec![directory])
        .await
        .unwrap_err()
        .to_string();
    assert!(error.contains("inexact_preimage"));

    for action_id in [
        "batch-absent-content",
        "batch-present-missing",
        "batch-directory-forged",
    ] {
        coordinator.abort(action_id).unwrap();
    }
    coordinator.shutdown_checked().await.unwrap();
}

#[test]
fn batch_protocol_round_trip_keeps_preimage_and_absent_markers() {
    let mut request = envelope().request();
    request.modified_resource_ids.push(key("destination"));
    let wire = ServiceRequest::PrepareBatch {
        envelope: RequestEnvelope {
            protocol_version: 1,
            auth: AuthContext {
                actor_id: "actor".into(),
                account_id: "account".into(),
                token: "opaque-test-token".into(),
            },
            claimed_digest: String::new(),
            request,
        },
        batch_version: 1,
        entries: vec![BatchPrepareEntry {
            resource_key: key("destination"),
            old_locator: Some(Locator::from("dst/a.txt")),
            new_locator: Some(Locator::from("dst/a.txt")),
            expected_revision: Some("revision:destination".into()),
            existence: ResourceExistence::Absent,
            resource_type: ResourceType::File,
            metadata: ResourceMetadata::default(),
            content: None,
            staged_upload_id: None,
            fingerprint: "missing-before".into(),
            coverage: CaptureManifest::default(),
        }],
    };
    let encoded = serde_json::to_vec(&wire).unwrap();
    let decoded: ServiceRequest = serde_json::from_slice(&encoded).unwrap();
    assert_eq!(decoded, wire);
}

#[tokio::test]
async fn batch_expiry_removes_every_payload_root_after_reopen() {
    use sha2::{Digest, Sha256};
    use openclank_history::catalog::{LiveReceipt, LiveStatus, ResourceOutcome, VersionContent};
    use openclank_history::operations::BatchAfterInput;
    use openclank_history::retention::ExpiryState;
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(root.path().join("catalog"), root.path().join("lore"), "account").await.unwrap();
    let adapter = CaptureAdapter::new(&coordinator);
    adapter.prepare_batch(envelope(), vec![
        entry("source", "source", "source", ResourceExistence::Present, Some(b"source-before")),
        entry("destination", "destination", "destination", ResourceExistence::Present, Some(b"destination-before")),
        entry("created", "created", "created", ResourceExistence::Absent, None),
    ]).await.unwrap();
    coordinator.begin_apply("batch-1").unwrap();
    coordinator.record_live("batch-1", LiveReceipt { committed_resources: Vec::new(), action_id: "batch-1".into(), status: LiveStatus::Committed, fingerprint: None }).unwrap();
    let after = ["source", "destination", "created"].into_iter().map(|id| {
        let content = format!("{id}-after");
        let fingerprint = format!("sha256:{:x}:{}", Sha256::digest(content.as_bytes()), content.len());
        BatchAfterInput {
            resource_key: key(id), locator: Some(Locator::from(id)), existence: ResourceExistence::Present,
            resource_type: ResourceType::File, metadata: ResourceMetadata { mode: Some(0o640), size: Some(content.len() as u64), modified_millis: Some(7), opaque: None },
            content: Some(Bytes::from(content)), fingerprint: fingerprint.clone(),
            coverage: CaptureManifest { metadata: Some(serde_json::json!({"exact_after":true})), ..Default::default() },
            outcome: ResourceOutcome { resource_id: id.into(), status: "Committed".into(), revision: Some(openclank_history::catalog::Revision::Opaque { kind: "fingerprint".into(), value: fingerprint }) },
        }
    }).collect();
    let record = coordinator.capture_after_batch("batch-1", after).await.unwrap();
    coordinator.complete("batch-1").unwrap();
    let partition = coordinator.catalog().owner_partition("account").unwrap();
    let receipts = record.before_resources.iter().filter_map(|r| r.before.as_ref()).chain(record.after_resources.iter().map(|r| &r.after)).cloned().collect::<Vec<_>>();
    assert_eq!(coordinator.reclaim_checked("batch-1", 99).await.unwrap().state, ExpiryState::Expired);
    coordinator.shutdown_checked().await.unwrap();
    drop(adapter); drop(coordinator);
    let store = openclank_history::HistoryStore::open(root.path().join("lore")).await.unwrap();
    for receipt in receipts {
        if let VersionContent::Bytes(reference) = receipt.content {
            let address = lore_storage::Address { context: lore_storage::Context::from(reference.context), hash: reference.hash_hex.parse().unwrap() };
            assert!(store.read(lore_storage::Partition::from(partition), address).await.is_err());
        }
    }
}
