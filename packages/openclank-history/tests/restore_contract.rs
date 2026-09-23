use bytes::Bytes;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use openclank_history::capture::{CaptureAdapter, CaptureEnvelope, CoverageKind, CoverageReceipt};
use openclank_history::catalog::{ActionState, LiveReceipt, LiveStatus, Locator, ResourceKey};
use openclank_history::operations::HistoryCoordinator;
use openclank_history::restore::{
    CurrentStateReceipt, HostMetadata, RestoreJournal, RestoreOutcome, RestoreProvider, RestoreRequest,
    RestoreState,
    apply_restore, apply_restore_batch_authorized, prepare_restore, prepare_restore_authorized,
};
use openclank_history::retention::{PolicySet, ScopeKind, ScopePolicy};
use tempfile::tempdir;
use std::fs;

fn envelope(action_id: &str, resource: &str) -> CaptureEnvelope {
    CaptureEnvelope {
        action_id: action_id.into(),
        actor_id: "actor".into(),
        actor_account_id: "account".into(),
        actor_kind: "agent".into(),
        session_id: None,
        run_id: None,
        task_id: None,
        tool_id: Some("restore-test".into()),
        resource_key: ResourceKey {
            account_id: "account".into(),
            workspace_id: "workspace".into(),
            provider: "copal".into(),
            resource_id: resource.into(),
        },
        guard_resource_ids: Vec::new(),
        modified_resource_ids: Vec::new(),
        operation: "replace".into(),
        expected_revision: None,
        before_revision: None,
        expected_after_revision: None,
        original_locator: Some(Locator::from(resource)),
        destination_locator: None,
        per_resource_outcomes: None,
        coverage: CoverageReceipt {
            kind: CoverageKind::KnownMutationHooks,
            captured_at_millis: 1,
            roots: Vec::new(),
            exclusions: Vec::new(),
        },
    }
}

#[tokio::test]
async fn exact_version_restore_requires_current_capture_and_keeps_provenance() {
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let adapter = CaptureAdapter::new(&coordinator);
    let before = adapter
        .prepare(
            envelope("action-1", "doc"),
            Some(Bytes::from_static(b"before")),
            "before",
        )
        .await
        .unwrap();
    adapter
        .finish(
            "action-1",
            LiveReceipt {
                action_id: "action-1".into(),
                status: LiveStatus::Committed,
                fingerprint: Some("after".into()),
            },
            Some(Bytes::from_static(b"after")),
            "after",
        )
        .await
        .unwrap();
    let source = before.before.unwrap();
    let plan = prepare_restore(
        &coordinator,
        "action-1",
        &source,
        ResourceKey {
            account_id: "account".into(),
            workspace_id: "workspace".into(),
            provider: "copal".into(),
            resource_id: "doc".into(),
        },
        Some("after"),
        Some("after"),
        true,
    )
    .await
    .unwrap();
    assert!(
        prepare_restore(
            &coordinator,
            "action-1",
            &source,
            ResourceKey {
                account_id: "account".into(),
                workspace_id: "workspace".into(),
                provider: "copal".into(),
                resource_id: "doc".into(),
            },
            Some("after"),
            Some("newer-external-edit"),
            true,
        )
        .await
        .is_err()
    );
    assert!(
        apply_restore(
            &plan,
            Some(&CurrentStateReceipt {
                version_id: "current".into(),
                fingerprint: "current".into(),
                content: b"current".to_vec(),
                durable: false,
                host_metadata: HostMetadata::default(),
            }),
            |_| Ok(())
        )
        .is_err()
    );
    let mut restored = Vec::new();
    let receipt = apply_restore(
        &plan,
        Some(&CurrentStateReceipt {
            version_id: "current".into(),
            fingerprint: "current".into(),
            content: b"current".to_vec(),
                durable: true,
                host_metadata: HostMetadata::default(),
        }),
        |bytes| {
            restored.extend_from_slice(bytes);
            Ok(())
        },
    )
    .unwrap();
    assert_eq!(receipt.outcome, RestoreOutcome::Complete);
    assert_eq!(receipt.source_action_id, "action-1");
    assert_eq!(restored, b"before");
    assert_eq!(
        coordinator
            .catalog()
            .get_action("action-1")
            .unwrap()
            .unwrap()
            .state,
        ActionState::Complete
    );
}

struct CasProvider {
    fingerprint: Option<String>,
    bytes: Vec<u8>,
    captures: usize,
    applies: usize,
    receipt: Option<(String, String)>,
}

impl RestoreProvider for CasProvider {
    fn current_fingerprint(&self) -> openclank_history::catalog::CatalogResult<Option<String>> {
        Ok(self.fingerprint.clone())
    }

    fn capture_current(&mut self) -> openclank_history::catalog::CatalogResult<CurrentStateReceipt> {
        self.captures += 1;
        Ok(CurrentStateReceipt {
            version_id: "current-proof".into(),
            fingerprint: self.fingerprint.clone().unwrap_or_default(),
            content: self.bytes.clone(),
            durable: true,
            host_metadata: HostMetadata::default(),
        })
    }

    fn apply_if_revision(
        &mut self,
        expected_fingerprint: Option<&str>,
        content: &[u8],
    ) -> openclank_history::catalog::CatalogResult<()> {
        if expected_fingerprint != self.fingerprint.as_deref() {
            return Err("CAS conflict".into());
        }
        self.bytes = content.to_vec();
        self.fingerprint = Some("restored".into());
        self.applies += 1;
        Ok(())
    }

    fn reconcile_apply(&self, restore_id: &str, content_digest: &str) -> openclank_history::catalog::CatalogResult<bool> {
        Ok(self.receipt.as_ref() == Some(&(restore_id.to_owned(), content_digest.to_owned())))
    }

    fn apply_if_revision_idempotent(
        &mut self,
        restore_id: &str,
        expected_fingerprint: Option<&str>,
        content: &[u8],
    ) -> openclank_history::catalog::CatalogResult<()> {
        if self.reconcile_apply(restore_id, &blake3::hash(content).to_string())? {
            return Ok(());
        }
        self.apply_if_revision(expected_fingerprint, content)?;
        self.receipt = Some((restore_id.into(), blake3::hash(content).to_string()));
        Ok(())
    }
}

#[tokio::test]
async fn authorized_restore_persists_proof_and_is_idempotent_after_reopenable_journal() {
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let adapter = CaptureAdapter::new(&coordinator);
    let before = adapter
        .prepare(envelope("action-restore", "doc"), Some(Bytes::from_static(b"before")), "before")
        .await
        .unwrap();
    adapter
        .finish(
            "action-restore",
            LiveReceipt {
                action_id: "action-restore".into(),
                status: LiveStatus::Committed,
                fingerprint: Some("after".into()),
            },
            Some(Bytes::from_static(b"after")),
            "after",
        )
        .await
        .unwrap();
    let source = before.before.unwrap();
    let request = RestoreRequest {
        restore_id: "restore-1".into(),
        account_id: "account".into(),
        source_action_id: "action-restore".into(),
        source_version_id: source.version_id.clone(),
        destination: envelope("unused", "doc").resource_key,
        expected_destination_fingerprint: Some("after".into()),
        require_current_capture: true,
    };
    let plan = prepare_restore_authorized(&coordinator, &request, &source, Some("after"))
        .await
        .unwrap();
    let mut provider = CasProvider {
        fingerprint: Some("after".into()),
        bytes: b"after".to_vec(),
        captures: 0,
        applies: 0,
        receipt: None,
    };
    let receipt = openclank_history::restore::apply_restore_authorized_durable(
        &coordinator,
        &request,
        &plan,
        &mut provider,
    )
    .await
    .unwrap();
    assert_eq!(receipt.outcome, RestoreOutcome::Complete);
    assert_eq!(provider.bytes, b"before");
    assert_eq!(provider.captures, 1);
    let mut interrupted = coordinator
        .catalog()
        .get_restore::<RestoreJournal>("restore-1")
        .unwrap()
        .unwrap();
    interrupted.state = RestoreState::Applying;
    interrupted.outcome = None;
    coordinator.catalog().put_restore("restore-1", &interrupted).unwrap();
    drop(coordinator);
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let again = openclank_history::restore::apply_restore_authorized_durable(
        &coordinator,
        &request,
        &plan,
        &mut provider,
    )
    .await
    .unwrap();
    assert_eq!(again.outcome, RestoreOutcome::Complete);
    assert_eq!(provider.captures, 1);
}

#[test]
fn host_file_adapter_round_trips_mode_and_reconciles_after_reopen() {
    let root = tempdir().unwrap();
    let path = root.path().join("note.md");
    fs::write(&path, b"current").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    let receipt_root = root.path().join("history-receipts");
    let mut provider = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&path, &receipt_root);
    let captured = provider.capture_current().unwrap();
    assert_eq!(captured.content, b"current");
    assert!(captured.host_metadata.native_locator.is_some());
    provider
        .apply_if_revision_idempotent_with_metadata(
            "restore-host",
            Some(&captured.fingerprint),
            b"restored",
            Some(&HostMetadata { mode: Some(0o644), ..HostMetadata::default() }),
        )
        .unwrap();
    drop(provider);
    let reopened = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&path, &receipt_root);
    let digest = blake3::hash(b"restored").to_string();
    assert!(reopened
        .reconcile_apply("restore-host", &digest)
        .unwrap());
    assert_eq!(fs::read(&path).unwrap(), b"restored");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o644);
    }
}

#[test]
fn directory_manifest_restores_recursive_files_and_links() {
    let root = tempdir().unwrap();
    let path = root.path().join("tree");
    std::fs::create_dir_all(path.join("nested")).unwrap();
    std::fs::write(path.join("nested/data.bin"), b"directory-bytes").unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink("nested/data.bin", path.join("alias")).unwrap();
    let receipt_root = root.path().join("receipts");
    let mut provider = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&path, &receipt_root);
    let captured = provider.capture_current().unwrap();
    assert_eq!(captured.host_metadata.resource_type.as_deref(), Some("Directory"));
    std::fs::remove_dir_all(&path).unwrap();
    provider
        .apply_if_revision_idempotent_with_metadata(
            "restore-directory",
            None,
            &captured.content,
            Some(&captured.host_metadata),
        )
        .unwrap();
    assert_eq!(std::fs::read(path.join("nested/data.bin")).unwrap(), b"directory-bytes");
    #[cfg(unix)]
    assert_eq!(std::fs::read_link(path.join("alias")).unwrap().to_string_lossy(), "nested/data.bin");
}

#[test]
fn directory_manifest_rejects_symlink_ancestor_before_writing() {
    let root = tempdir().unwrap();
    let path = root.path().join("tree");
    let outside = root.path().join("outside");
    std::fs::create_dir_all(&outside).unwrap();
    let receipt_root = root.path().join("receipts");
    let manifest = serde_json::json!({
        "version": 1,
        "root_type": "directory",
        "entries": [
            {"path": "escape", "type": "symlink", "target": "../outside"},
            {"path": "escape/created.txt", "type": "file", "content": STANDARD.encode(b"must not write")}
        ]
    });
    let mut provider = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&path, &receipt_root);
    let error = provider
        .apply_if_revision_idempotent_with_metadata(
            "restore-hostile-directory",
            None,
            &serde_json::to_vec(&manifest).unwrap(),
            Some(&HostMetadata { resource_type: Some("Directory".into()), ..HostMetadata::default() }),
        )
        .unwrap_err();
    assert!(error.to_string().contains("symlink ancestor"));
    assert!(!outside.join("created.txt").exists());
    assert!(!path.exists());
}

#[test]
fn host_receipts_are_hidden_and_collision_free_for_similar_extensions() {
    let root = tempdir().unwrap();
    let markdown = root.path().join("note.md");
    let text = root.path().join("note.txt");
    fs::write(&markdown, b"md-before").unwrap();
    fs::write(&text, b"txt-before").unwrap();
    let receipt_root = root.path().join("service-owned-receipts");
    let mut markdown_provider = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&markdown, &receipt_root);
    let mut text_provider = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&text, &receipt_root);
    let markdown_fp = markdown_provider.current_fingerprint().unwrap();
    let text_fp = text_provider.current_fingerprint().unwrap();
    markdown_provider
        .apply_if_revision_idempotent("restore-md", markdown_fp.as_deref(), b"md-after")
        .unwrap();
    text_provider
        .apply_if_revision_idempotent("restore-txt", text_fp.as_deref(), b"txt-after")
        .unwrap();
    assert_eq!(fs::read(&markdown).unwrap(), b"md-after");
    assert_eq!(fs::read(&text).unwrap(), b"txt-after");
    let receipts: Vec<_> = fs::read_dir(receipt_root).unwrap().collect();
    assert_eq!(receipts.len(), 2);
    assert!(receipts.iter().all(|entry| entry.as_ref().unwrap().file_name().to_string_lossy().ends_with(".receipt")));
    assert!(!root.path().join(".openclank-restore-md").exists());
}

#[test]
fn tampered_service_receipt_does_not_reconcile() {
    let root = tempdir().unwrap();
    let path = root.path().join("note.md");
    let receipt_root = root.path().join("receipts");
    fs::write(&path, b"before").unwrap();
    let mut provider = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&path, &receipt_root);
    let fingerprint = provider.current_fingerprint().unwrap();
    provider.apply_if_revision_idempotent("restore-tamper", fingerprint.as_deref(), b"after").unwrap();
    let receipt = fs::read_dir(&receipt_root).unwrap().next().unwrap().unwrap().path();
    fs::write(receipt, b"v1\nforged\nrestore-tamper\nnot-the-digest").unwrap();
    assert!(!provider.reconcile_apply("restore-tamper", &blake3::hash(b"after").to_string()).unwrap());
}

#[test]
fn missing_host_destination_captures_a_durable_tombstone() {
    let root = tempdir().unwrap();
    let path = root.path().join("new.md");
    let mut provider = openclank_history::restore::FilesystemRestoreProvider::new(&path);
    let receipt = provider.capture_current().unwrap();
    assert_eq!(receipt.version_id, "host:missing");
    assert_eq!(receipt.fingerprint, "missing");
    assert!(receipt.durable);
}

#[tokio::test]
async fn capture_usage_attribution_is_frozen_across_move_policy_edit_and_reopen() {
    let root = tempdir().unwrap();
    let scope_root = root.path().join("scope-a");
    std::fs::create_dir_all(&scope_root).unwrap();
    let mut coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let mut policy = PolicySet::default();
    policy.scopes.push(ScopePolicy {
        scope_id: "dir-a".into(),
        kind: ScopeKind::Directory,
        owner_account_id: Some("account".into()),
        workspace_id: Some("workspace".into()),
        root: Some(scope_root.clone()),
        limit_bytes: Some(100_000),
        revision: 1,
        enabled: true,
    });
    coordinator.set_policy(1, policy).unwrap();
    let mut capture = CaptureAdapter::new(&coordinator);
    let mut request = envelope("frozen-usage", "doc");
    request.original_locator = Some(Locator::from(scope_root.join("old.md").to_string_lossy().to_string()));
    request.destination_locator = request.original_locator.clone();
    capture
        .prepare(request, Some(Bytes::from_static(b"old")), "old")
        .await
        .unwrap();
    capture
        .finish(
            "frozen-usage",
            LiveReceipt {
                action_id: "frozen-usage".into(),
                status: LiveStatus::Committed,
                fingerprint: Some("new".into()),
            },
            Some(Bytes::from_static(b"new-content")),
            "new",
        )
        .await
        .unwrap();
    let usage_before = coordinator.usage().unwrap();
    let retained = usage_before.scopes.iter().find(|scope| scope.scope_id == "dir-a").unwrap();
    assert_eq!(retained.logical_retained_bytes, 3 + 11);
    let mut edited = coordinator.policy_set().unwrap();
    edited.scopes[0].limit_bytes = Some(200_000);
    coordinator.set_policy(edited.revision, edited).unwrap();
    let usage_after_edit = coordinator.usage().unwrap();
    assert_eq!(usage_after_edit.scopes, usage_before.scopes);
    drop(capture);
    drop(coordinator);
    let reopened = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    assert_eq!(reopened.usage().unwrap().scopes, usage_before.scopes);
}

#[tokio::test]
async fn batch_restore_reports_partial_per_resource_outcomes_without_overwrite() {
    let root = tempdir().unwrap();
    let coordinator = HistoryCoordinator::open(
        root.path().join("catalog"),
        root.path().join("lore"),
        "account",
    )
    .await
    .unwrap();
    let adapter = CaptureAdapter::new(&coordinator);
    let _before = adapter
        .prepare(
            envelope("batch-source", "source"),
            Some(Bytes::from_static(b"before")),
            "before",
        )
        .await
        .unwrap();
    adapter
        .finish(
            "batch-source",
            LiveReceipt {
                action_id: "batch-source".into(),
                status: LiveStatus::Committed,
                fingerprint: Some("source".into()),
            },
            Some(Bytes::from_static(b"source")),
            "source",
        )
        .await
        .unwrap();
    let source = coordinator
        .catalog()
        .get_action("batch-source")
        .unwrap()
        .unwrap()
        .after
        .unwrap();
    adapter
        .prepare(
            envelope("batch-source-b", "source-b"),
            Some(Bytes::from_static(b"before")),
            "before-b",
        )
        .await
        .unwrap();
    adapter
        .finish(
            "batch-source-b",
            LiveReceipt {
                action_id: "batch-source-b".into(),
                status: LiveStatus::Committed,
                fingerprint: Some("source-b".into()),
            },
            Some(Bytes::from_static(b"source")),
            "source-b",
        )
        .await
        .unwrap();
    let source_b = coordinator
        .catalog()
        .get_action("batch-source-b")
        .unwrap()
        .unwrap()
        .after
        .unwrap();
    let request_a = RestoreRequest {
        restore_id: "batch-a".into(),
        account_id: "account".into(),
        source_action_id: "batch-source".into(),
        source_version_id: source.version_id.clone(),
        destination: ResourceKey { resource_id: "target-a".into(), ..envelope("unused", "target-a").resource_key },
        expected_destination_fingerprint: None,
        require_current_capture: true,
    };
    let request_b = RestoreRequest {
        restore_id: "batch-b".into(),
        source_action_id: "batch-source-b".into(),
        source_version_id: source_b.version_id.clone(),
        destination: ResourceKey { resource_id: "target-b".into(), ..envelope("unused", "target-b").resource_key },
        ..request_a.clone()
    };
    let path_a = root.path().join("target-a");
    let path_b = root.path().join("target-b");
    fs::write(&path_b, b"external-edit").unwrap();
    let plan_a = prepare_restore_authorized(&coordinator, &request_a, &source, None)
        .await
        .unwrap();
    let plan_b = prepare_restore_authorized(&coordinator, &request_b, &source_b, None)
        .await
        .unwrap();
    let receipt_root = root.path().join("receipts");
    let mut provider_a = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&path_a, &receipt_root);
    let mut provider_b = openclank_history::restore::FilesystemRestoreProvider::new_with_receipt_root(&path_b, &receipt_root);
    let mut entries: [(&RestoreRequest, &openclank_history::restore::RestorePlan, &mut dyn RestoreProvider); 2] = [
        (&request_a, &plan_a, &mut provider_a),
        (&request_b, &plan_b, &mut provider_b),
    ];
    let result = apply_restore_batch_authorized(&coordinator, &mut entries).await;
    assert_eq!(result.outcome, RestoreOutcome::Partial);
    assert_eq!(result.resources[0].outcome, RestoreOutcome::Complete);
    assert_eq!(result.resources[1].outcome, RestoreOutcome::Conflict);
    assert_eq!(fs::read(&path_a).unwrap(), b"source");
    assert_eq!(fs::read(&path_b).unwrap(), b"external-edit");
}
