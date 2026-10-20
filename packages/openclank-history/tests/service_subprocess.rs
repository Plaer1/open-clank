#![cfg(unix)]

use openclank_history::catalog::{
    ActionRecord, ActionState, CaptureManifest, LiveReceipt, LiveStatus, Locator, LoreRef,
    ResourceExistence, ResourceKey, ResourceMetadata, ResourceType, VersionContent, VersionReceipt,
};
use openclank_history::operations::{request_digest, ActionRequest};
use openclank_history::protocol::{
    AuthContext, BatchPrepareEntry, ControlEnvelope, RequestEnvelope, ServiceRequest,
    ServiceResponse, PROTOCOL_VERSION,
};
use openclank_history::restore::{
    FilesystemRestoreProvider, RestoreOutcome, RestoreProvider, RestoreRequest,
};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

fn request() -> ActionRequest {
    ActionRequest {
        schema_version: 1,
        action_id: "service-action".into(),
        actor_account_id: "account".into(),
        resource_key: openclank_history::catalog::ResourceKey {
            account_id: "account".into(),
            workspace_id: "workspace".into(),
            provider: "files".into(),
            resource_id: "resource".into(),
        },
        physical_lease_keys: vec![],
        guard_resource_ids: vec![],
        modified_resource_ids: vec![],
        operation: "replace".into(),
        expected_revision: Some("r1".into()),
        actor_id: "actor".into(),
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

fn control(action_id: &str) -> ControlEnvelope {
    control_as(action_id, "actor", "account", "token")
}

fn control_as(action_id: &str, actor_id: &str, account_id: &str, token: &str) -> ControlEnvelope {
    ControlEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: actor_id.into(),
            account_id: account_id.into(),
            token: token.into(),
        },
        action_id: action_id.into(),
    }
}

fn sha256_fingerprint(content: &[u8]) -> String {
    let mut digest = Sha256::new();
    digest.update(content);
    format!("sha256:{:x}:{}", digest.finalize(), content.len())
}

async fn read_version_chunked(
    stream: &mut (
        tokio::net::unix::OwnedReadHalf,
        tokio::net::unix::OwnedWriteHalf,
    ),
    action_id: &str,
    receipt: VersionReceipt,
) -> Vec<u8> {
    let info = send(
        stream,
        ServiceRequest::ReadVersionInfo {
            envelope: control(action_id),
            receipt: receipt.clone(),
        },
    )
    .await;
    let content_length = match info {
        ServiceResponse::VersionInfo {
            content_length: Some(length),
        } => length,
        other => panic!("unexpected version metadata: {other:?}"),
    };
    let mut content = Vec::new();
    let mut offset = 0_u64;
    while offset < content_length {
        let response = send(
            stream,
            ServiceRequest::ReadVersionChunk {
                envelope: control(action_id),
                receipt: receipt.clone(),
                offset,
                length: 512 * 1024,
            },
        )
        .await;
        match response {
            ServiceResponse::Chunk {
                offset: returned,
                content: Some(bytes),
                eof,
            } => {
                assert_eq!(returned, offset);
                assert!(!bytes.is_empty());
                content.extend_from_slice(&bytes);
                offset += bytes.len() as u64;
                assert_eq!(eof, offset == content_length);
            }
            other => panic!("unexpected version chunk: {other:?}"),
        }
    }
    assert_eq!(content.len() as u64, content_length);
    content
}

async fn send(
    stream: &mut (
        tokio::net::unix::OwnedReadHalf,
        tokio::net::unix::OwnedWriteHalf,
    ),
    request: ServiceRequest,
) -> ServiceResponse {
    stream
        .1
        .write_all(serde_json::to_string(&request).unwrap().as_bytes())
        .await
        .unwrap();
    stream.1.write_all(b"\n").await.unwrap();
    let mut line = String::new();
    BufReader::new(&mut stream.0)
        .read_line(&mut line)
        .await
        .unwrap();
    serde_json::from_str(&line).unwrap()
}

async fn stage_small(
    stream: &mut (
        tokio::net::unix::OwnedReadHalf,
        tokio::net::unix::OwnedWriteHalf,
    ),
    action_id: &str,
    upload_id: &str,
    content: &[u8],
) {
    assert!(matches!(
        send(
            stream,
            ServiceRequest::StageBegin {
                envelope: control(action_id),
                upload_id: upload_id.to_owned(),
                content_length: content.len() as u64,
                fingerprint: sha256_fingerprint(content),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            stream,
            ServiceRequest::StageChunk {
                envelope: control(action_id),
                upload_id: upload_id.to_owned(),
                offset: 0,
                content: Some(content.to_vec()),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            stream,
            ServiceRequest::StageFinish {
                envelope: control(action_id),
                upload_id: upload_id.to_owned(),
            },
        )
        .await,
        ServiceResponse::Staged { .. }
    ));
}

async fn send_maybe(
    stream: &mut (
        tokio::net::unix::OwnedReadHalf,
        tokio::net::unix::OwnedWriteHalf,
    ),
    request: ServiceRequest,
) -> Option<ServiceResponse> {
    if stream
        .1
        .write_all(serde_json::to_string(&request).ok()?.as_bytes())
        .await
        .is_err()
    {
        return None;
    }
    if stream.1.write_all(b"\n").await.is_err() {
        return None;
    }
    let mut line = String::new();
    let read = BufReader::new(&mut stream.0)
        .read_line(&mut line)
        .await
        .ok()?;
    if read == 0 {
        return None;
    }
    serde_json::from_str(&line).ok()
}

async fn connect(
    socket: &std::path::Path,
) -> (
    tokio::net::unix::OwnedReadHalf,
    tokio::net::unix::OwnedWriteHalf,
) {
    loop {
        match UnixStream::connect(socket).await {
            Ok(stream) => return stream.into_split(),
            Err(_) => tokio::time::sleep(std::time::Duration::from_millis(10)).await,
        }
    }
}

#[tokio::test]
async fn settings_ipc_is_authenticated_and_persists_across_worker_restart() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("settings.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let spawn = || {
        std::process::Command::new(bin)
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "account",
            ])
            .env("OPENCLANK_HISTORY_TOKEN", "token")
            .spawn()
            .unwrap()
    };
    let mut child = spawn();
    let mut stream = connect(&socket).await;
    let policy = match send(&mut stream, ServiceRequest::GetPolicy(control("settings"))).await {
        ServiceResponse::Policy(policy) => policy,
        other => panic!("unexpected policy response: {other:?}"),
    };
    assert_eq!(policy.revision, 1);
    let mut changed = policy.clone();
    changed.global.total_bytes = 4096;
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::SetPolicy {
                envelope: control("settings"),
                expected_revision: 1,
                policy: changed,
            },
        )
        .await,
        ServiceResponse::Policy(_)
    ));
    assert!(matches!(
        send(&mut stream, ServiceRequest::GetUsage(control("settings"))).await,
        ServiceResponse::Usage(_)
    ));
    assert!(matches!(
        send(&mut stream, ServiceRequest::GetStatus(control("settings"))).await,
        ServiceResponse::Status { .. }
    ));
    let mut unauthorized = control("settings");
    unauthorized.auth.token = "wrong".into();
    assert!(matches!(
        send(&mut stream, ServiceRequest::GetPolicy(unauthorized)).await,
        ServiceResponse::Error { .. }
    ));
    let mut second_account = control("settings");
    second_account.auth.account_id = "acct-bob".into();
    assert!(matches!(
        send(&mut stream, ServiceRequest::GetPolicy(second_account)).await,
        ServiceResponse::Error { .. }
    ));
    child.kill().unwrap();
    let _ = child.wait();
    let mut reopened = spawn();
    let mut stream = connect(&socket).await;
    match send(&mut stream, ServiceRequest::GetPolicy(control("settings"))).await {
        ServiceResponse::Policy(policy) => assert_eq!(policy.global.total_bytes, 4096),
        other => panic!("unexpected reopened policy response: {other:?}"),
    }
    assert!(matches!(
        send(&mut stream, ServiceRequest::Shutdown(control("settings"))).await,
        ServiceResponse::Accepted
    ));
    let _ = reopened.wait();
}

#[tokio::test]
async fn server_issued_credentials_isolate_two_account_partitions() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("credentials.sock");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let credentials = serde_json::json!([
        {"actor_id":"*","account_id":"alice","token":"alice-token","capabilities":["capture","admin"]},
        {"actor_id":"bob-session","account_id":"bob","token":"bob-token","capabilities":["read","settings-read"]}
    ]);
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_CREDENTIALS", credentials.to_string())
        .spawn()
        .unwrap();
    let mut stream = connect(&socket).await;
    let mut alice_action = request();
    alice_action.action_id = "alice-action".into();
    alice_action.actor_account_id = "alice".into();
    alice_action.resource_key.account_id = "alice".into();
    alice_action.actor_id = "alice-session".into();
    let prepared = send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: RequestEnvelope {
                protocol_version: PROTOCOL_VERSION,
                auth: AuthContext {
                    actor_id: "alice-session".into(),
                    account_id: "alice".into(),
                    token: "alice-token".into(),
                },
                claimed_digest: String::new(),
                request: alice_action,
            },
            content: Some(b"alice-before".to_vec()),
            fingerprint: "alice-before".into(),
        },
    )
    .await;
    assert!(matches!(prepared, ServiceResponse::Action(_)));

    let forged = send(
        &mut stream,
        ServiceRequest::RecordLive {
            envelope: control_as("alice-action", "bob-session", "bob", "bob-token"),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: "alice-action".into(),
                status: LiveStatus::Committed,
                fingerprint: Some("alice-after".into()),
            },
        },
    )
    .await;
    assert!(matches!(forged, ServiceResponse::Error { .. }));

    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::GetPolicy(control_as(
                "bob-settings",
                "bob-session",
                "bob",
                "bob-token"
            )),
        )
        .await,
        ServiceResponse::Policy(_)
    ));
    let bob_shutdown = send(
        &mut stream,
        ServiceRequest::Shutdown(control_as(
            "bob-settings",
            "bob-session",
            "bob",
            "bob-token",
        )),
    )
    .await;
    assert!(matches!(bob_shutdown, ServiceResponse::Error { .. }));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Abort {
                envelope: control_as("alice-action", "alice-session", "alice", "alice-token",),
            },
        )
        .await,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    let shutdown = send(
        &mut stream,
        ServiceRequest::Shutdown(control_as(
            "alice-action",
            "alice-session",
            "alice",
            "alice-token",
        )),
    )
    .await;
    assert!(
        matches!(shutdown, ServiceResponse::Accepted),
        "{shutdown:?}"
    );
    child.wait().unwrap();
}

#[tokio::test]
async fn resource_registry_is_authenticated_persistent_and_tombstones_recreated_paths() {
    let dir = tempfile::tempdir().unwrap();
    let workspace = dir.path().join("workspace");
    let notes = workspace.join("notes");
    std::fs::create_dir_all(&notes).unwrap();
    std::fs::write(notes.join("entry.md"), b"before").unwrap();
    let socket = dir.path().join("registry.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let resource_map = dir.path().join("resource-map.json");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let credentials = serde_json::json!([
        {"actor_id":"*","account_id":"account-a","token":"token-a","capabilities":["admin","capture","read","restore"]},
        {"actor_id":"bob","account_id":"account-b","token":"token-b","capabilities":["read"]}
    ]);
    let spawn = || {
        std::process::Command::new(bin)
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "",
                workspace.to_str().unwrap(),
                dir.path().join("receipts").to_str().unwrap(),
                resource_map.to_str().unwrap(),
            ])
            .env("OPENCLANK_HISTORY_CREDENTIALS", credentials.to_string())
            .spawn()
            .unwrap()
    };
    let mut child = spawn();
    let mut stream = connect(&socket).await;
    let register = send(
        &mut stream,
        ServiceRequest::RegisterResource {
            envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
            account_id: "account-a".into(),
            workspace_id: "workspace-a".into(),
            root_id: None,
            root_path: workspace.to_string_lossy().into_owned(),
            relative_path: "notes/entry.md".into(),
            resource_id: None,
        },
    )
    .await;
    let (resource_id, generation) = match register {
        ServiceResponse::Resource { handle, created } => {
            assert!(created);
            (handle.resource_id, handle.generation)
        }
        other => panic!("unexpected registration response: {other:?}"),
    };
    let repeated = send(
        &mut stream,
        ServiceRequest::RegisterResource {
            envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
            account_id: "account-a".into(),
            workspace_id: "workspace-a".into(),
            root_id: None,
            root_path: workspace.to_string_lossy().into_owned(),
            relative_path: "notes/entry.md".into(),
            resource_id: None,
        },
    )
    .await;
    assert!(matches!(
        repeated,
        ServiceResponse::Resource { created: false, .. }
    ));
    let renamed = send(
        &mut stream,
        ServiceRequest::UpdateResource {
            envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
            resource_id: resource_id.clone(),
            account_id: "account-a".into(),
            workspace_id: "workspace-a".into(),
            root_id: None,
            root_path: workspace.to_string_lossy().into_owned(),
            relative_path: "notes/renamed.md".into(),
        },
    )
    .await;
    match renamed {
        ServiceResponse::Resource { handle, created } => {
            assert!(!created);
            assert_eq!(handle.resource_id, resource_id);
            assert!(handle.generation > generation);
        }
        other => panic!("unexpected rename response: {other:?}"),
    }
    let mut bob = connect(&socket).await;
    assert!(matches!(
        send(
            &mut bob,
            ServiceRequest::ResolveResource {
                envelope: control_as("resource-registry", "bob", "account-b", "token-b"),
                resource_id: resource_id.clone(),
            },
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::RevokeResource {
                envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
                resource_id: resource_id.clone(),
                reason: Some("deleted".into()),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    let recreated = send(
        &mut stream,
        ServiceRequest::RegisterResource {
            envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
            account_id: "account-a".into(),
            workspace_id: "workspace-a".into(),
            root_id: None,
            root_path: workspace.to_string_lossy().into_owned(),
            relative_path: "notes/renamed.md".into(),
            resource_id: None,
        },
    )
    .await;
    let recreated_id = match recreated {
        ServiceResponse::Resource { handle, created } => {
            assert!(created);
            assert_ne!(handle.resource_id, resource_id);
            handle.resource_id
        }
        other => panic!("unexpected recreation response: {other:?}"),
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::ResolveResource {
                envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
                resource_id: resource_id.clone(),
            },
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    child.kill().unwrap();
    let _ = child.wait();
    let mut reopened = spawn();
    let mut reopened_stream = connect(&socket).await;
    match send(
        &mut reopened_stream,
        ServiceRequest::RegisterResource {
            envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
            account_id: "account-a".into(),
            workspace_id: "workspace-a".into(),
            root_id: None,
            root_path: workspace.to_string_lossy().into_owned(),
            relative_path: "notes/renamed.md".into(),
            resource_id: None,
        },
    )
    .await
    {
        ServiceResponse::Resource { handle, created } => {
            assert!(!created);
            assert_eq!(handle.resource_id, recreated_id);
        }
        other => panic!("unexpected persistent registration response: {other:?}"),
    }
    assert!(matches!(
        send(
            &mut reopened_stream,
            ServiceRequest::Shutdown(control_as(
                "resource-registry",
                "alice",
                "account-a",
                "token-a"
            )),
        )
        .await,
        ServiceResponse::Accepted
    ));
    let _ = reopened.wait();
}

#[tokio::test]
async fn resource_registry_move_atomically_replaces_destination_and_retains_source_id() {
    let dir = tempfile::tempdir().unwrap();
    let workspace = dir.path().join("workspace");
    std::fs::create_dir_all(&workspace).unwrap();
    std::fs::write(workspace.join("source.md"), b"source").unwrap();
    std::fs::write(workspace.join("destination.md"), b"destination").unwrap();
    let socket = dir.path().join("move.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let resource_map = dir.path().join("absolute-resource-map.json");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let credentials = serde_json::json!([
        {"actor_id":"*","account_id":"account-a","token":"token-a","capabilities":["admin","capture","read","restore"]}
    ]);
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            catalog.to_str().unwrap(),
            lore.to_str().unwrap(),
            "",
            workspace.to_str().unwrap(),
            dir.path().join("receipts").to_str().unwrap(),
            resource_map.to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_CREDENTIALS", credentials.to_string())
        .spawn()
        .unwrap();
    let mut stream = connect(&socket).await;
    let source = match send(
        &mut stream,
        ServiceRequest::RegisterResource {
            envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
            account_id: "account-a".into(),
            workspace_id: "workspace-a".into(),
            root_id: None,
            root_path: workspace.to_string_lossy().into_owned(),
            relative_path: "source.md".into(),
            resource_id: None,
        },
    )
    .await
    {
        ServiceResponse::Resource { handle, created } => {
            assert!(created);
            handle
        }
        other => panic!("unexpected source registration response: {other:?}"),
    };
    let destination = match send(
        &mut stream,
        ServiceRequest::RegisterResource {
            envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
            account_id: "account-a".into(),
            workspace_id: "workspace-a".into(),
            root_id: None,
            root_path: workspace.to_string_lossy().into_owned(),
            relative_path: "destination.md".into(),
            resource_id: None,
        },
    )
    .await
    {
        ServiceResponse::Resource { handle, created } => {
            assert!(created);
            handle
        }
        other => panic!("unexpected destination registration response: {other:?}"),
    };
    let moved = send(
        &mut stream,
        ServiceRequest::MoveResource {
            envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
            resource_id: source.resource_id.clone(),
            replaced_resource_id: Some(destination.resource_id.clone()),
            account_id: "account-a".into(),
            workspace_id: "workspace-a".into(),
            root_id: None,
            root_path: workspace.to_string_lossy().into_owned(),
            relative_path: "destination.md".into(),
        },
    )
    .await;
    match moved {
        ServiceResponse::Resource { handle, created } => {
            assert!(!created);
            assert_eq!(handle.resource_id, source.resource_id);
            assert!(handle.generation > source.generation);
        }
        other => panic!("unexpected move response: {other:?}"),
    }
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::ResolveResource {
                envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
                resource_id: destination.resource_id.clone(),
            },
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::ResolveResource {
                envelope: control_as("resource-registry", "alice", "account-a", "token-a"),
                resource_id: source.resource_id.clone(),
            },
        )
        .await,
        ServiceResponse::Resource { .. }
    ));
    let persisted: openclank_history::registry::ResourceRegistry =
        serde_json::from_slice(&std::fs::read(&resource_map).unwrap()).unwrap();
    let current = persisted.entries.get(&source.resource_id).unwrap();
    assert_eq!(current.relative_path, "destination.md");
    assert!(
        !persisted
            .entries
            .get(&destination.resource_id)
            .unwrap()
            .active
    );
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control_as(
                "resource-registry",
                "alice",
                "account-a",
                "token-a"
            )),
        )
        .await,
        ServiceResponse::Accepted
    ));
    let _ = child.wait();
}

#[tokio::test]
async fn staged_capture_handles_incompressible_payloads_beyond_one_frame_and_cleans_up() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("staged.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            catalog.to_str().unwrap(),
            lore.to_str().unwrap(),
            "account",
            dir.path().to_str().unwrap(),
            dir.path().join("receipts").to_str().unwrap(),
            dir.path().join("resource-map.json").to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .spawn()
        .unwrap();
    let mut stream = connect(&socket).await;
    let action = request();
    let request_envelope = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: String::new(),
        request: action.clone(),
    };
    let before: Vec<u8> = (0_u64..1_100_123)
        .map(|index| index.wrapping_mul(73) as u8)
        .collect();
    assert!(before.len() > 1024 * 1024);
    let upload_id = "stage-large-before".to_owned();
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("service-action"),
                upload_id: upload_id.clone(),
                content_length: before.len() as u64,
                fingerprint: sha256_fingerprint(&before),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    for (offset, chunk) in before.chunks(512 * 1024).enumerate() {
        let offset = offset * 512 * 1024;
        assert!(matches!(
            send(
                &mut stream,
                ServiceRequest::StageChunk {
                    envelope: control("service-action"),
                    upload_id: upload_id.clone(),
                    offset: offset as u64,
                    content: Some(chunk.to_vec()),
                },
            )
            .await,
            ServiceResponse::Accepted
        ));
    }
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageFinish {
                envelope: control("service-action"),
                upload_id: upload_id.clone(),
            },
        )
        .await,
        ServiceResponse::Staged { .. }
    ));
    let prepared = send(
        &mut stream,
        ServiceRequest::PrepareStaged {
            envelope: request_envelope,
            upload_id: upload_id.clone(),
            fingerprint: sha256_fingerprint(&before),
        },
    )
    .await;
    let before_receipt = match prepared {
        ServiceResponse::Action(record) => record.before.clone().unwrap(),
        other => panic!("unexpected staged prepare response: {other:?}"),
    };
    let live = send(
        &mut stream,
        ServiceRequest::RecordLive {
            envelope: control("service-action"),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: "service-action".into(),
                status: LiveStatus::Committed,
                fingerprint: Some("live-after".into()),
            },
        },
    )
    .await;
    match live {
        ServiceResponse::Action(record) => assert_eq!(record.state, ActionState::Applied),
        other => panic!("unexpected staged live response: {other:?}"),
    }
    let after: Vec<u8> = (0_u64..1_200_321)
        .map(|index| index.wrapping_mul(31).wrapping_add(7) as u8)
        .collect();
    let after_upload_id = "stage-large-after".to_owned();
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("service-action"),
                upload_id: after_upload_id.clone(),
                content_length: after.len() as u64,
                fingerprint: sha256_fingerprint(&after),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    for (offset, chunk) in after.chunks(512 * 1024).enumerate() {
        let offset = offset * 512 * 1024;
        assert!(matches!(
            send(
                &mut stream,
                ServiceRequest::StageChunk {
                    envelope: control("service-action"),
                    upload_id: after_upload_id.clone(),
                    offset: offset as u64,
                    content: Some(chunk.to_vec()),
                },
            )
            .await,
            ServiceResponse::Accepted
        ));
    }
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageFinish {
                envelope: control("service-action"),
                upload_id: after_upload_id.clone(),
            },
        )
        .await,
        ServiceResponse::Staged { .. }
    ));
    let completed = send(
        &mut stream,
        ServiceRequest::CompleteStaged {
            envelope: control("service-action"),
            upload_id: after_upload_id,
            fingerprint: sha256_fingerprint(&after),
        },
    )
    .await;
    let after_receipt = match completed {
        ServiceResponse::Action(record) => record.after.clone().unwrap(),
        other => panic!("unexpected staged complete response: {other:?}"),
    };
    let read_before = read_version_chunked(&mut stream, "service-action", before_receipt).await;
    assert_eq!(read_before, before);
    let read_after = read_version_chunked(&mut stream, "service-action", after_receipt).await;
    assert_eq!(read_after, after);
    assert!(
        std::fs::read_dir(dir.path().join(".openclank-history-staging"))
            .unwrap()
            .next()
            .is_none()
    );
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control("service-action")),
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    // The capture credential is intentionally not an administrator; the
    // service remains alive until its test process is reaped.
    child.kill().unwrap();
    let _ = child.wait();
    let _ = action;
}

#[tokio::test]
async fn batch_service_cleans_consumed_stages_on_late_error_and_rejects_omissions() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("batch-stage-cleanup.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            catalog.to_str().unwrap(),
            lore.to_str().unwrap(),
            "account",
            dir.path().to_str().unwrap(),
            dir.path().join("receipts").to_str().unwrap(),
            dir.path().join("resource-map.json").to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .spawn()
        .unwrap();
    let mut stream = connect(&socket).await;
    let mut action = request();
    action.action_id = "batch-service-cleanup".into();
    let secondary = ResourceKey {
        account_id: "account".into(),
        workspace_id: "workspace".into(),
        provider: "files".into(),
        resource_id: "secondary".into(),
    };
    action.modified_resource_ids = vec![secondary.clone()];
    let request_envelope = |action: ActionRequest| RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: String::new(),
        request: action,
    };
    let staged = b"staged-before".to_vec();
    let upload_id = "batch-stage-late-entry".to_owned();
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-service-cleanup"),
                upload_id: upload_id.clone(),
                content_length: staged.len() as u64,
                fingerprint: sha256_fingerprint(&staged),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageChunk {
                envelope: control("batch-service-cleanup"),
                upload_id: upload_id.clone(),
                offset: 0,
                content: Some(staged.clone()),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageFinish {
                envelope: control("batch-service-cleanup"),
                upload_id: upload_id.clone(),
            },
        )
        .await,
        ServiceResponse::Staged { .. }
    ));
    let primary = action.resource_key.clone();
    let late_bad = BatchPrepareEntry {
        resource_key: secondary,
        old_locator: Some(Locator::from("secondary")),
        new_locator: Some(Locator::from("secondary")),
        expected_revision: Some("secondary-revision".into()),
        existence: ResourceExistence::Present,
        resource_type: ResourceType::File,
        metadata: ResourceMetadata {
            size: Some(1),
            ..ResourceMetadata::default()
        },
        content: Some(b"bad".to_vec()),
        staged_upload_id: None,
        fingerprint: "secondary-fingerprint".into(),
        coverage: CaptureManifest {
            byte_len: Some(99),
            ..CaptureManifest::default()
        },
    };
    let staged_entry = BatchPrepareEntry {
        resource_key: primary,
        old_locator: Some(Locator::from("primary")),
        new_locator: Some(Locator::from("primary")),
        expected_revision: Some("primary-revision".into()),
        existence: ResourceExistence::Present,
        resource_type: ResourceType::File,
        metadata: ResourceMetadata {
            size: Some(staged.len() as u64),
            ..ResourceMetadata::default()
        },
        content: None,
        staged_upload_id: Some(upload_id.clone()),
        fingerprint: sha256_fingerprint(&staged),
        coverage: CaptureManifest {
            byte_len: Some(staged.len() as u64),
            ..CaptureManifest::default()
        },
    };
    let response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: request_envelope(action.clone()),
            batch_version: 1,
            entries: vec![staged_entry, late_bad],
        },
    )
    .await;
    assert!(matches!(response, ServiceResponse::Error { .. }));

    // The first staged entry was authenticated and consumed before the late
    // coverage failure. Reusing its id proves the service removed it.
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-service-cleanup"),
                upload_id: upload_id.clone(),
                content_length: staged.len() as u64,
                fingerprint: sha256_fingerprint(&staged),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("batch-service-cleanup"),
                upload_id,
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    let bound_upload_id = "batch-stage-bound-error".to_owned();
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-service-cleanup"),
                upload_id: bound_upload_id.clone(),
                content_length: staged.len() as u64,
                fingerprint: sha256_fingerprint(&staged),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageChunk {
                envelope: control("batch-service-cleanup"),
                upload_id: bound_upload_id.clone(),
                offset: 0,
                content: Some(staged.clone()),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageFinish {
                envelope: control("batch-service-cleanup"),
                upload_id: bound_upload_id.clone(),
            },
        )
        .await,
        ServiceResponse::Staged { .. }
    ));
    let oversized_inline = vec![b'i'; 513 * 1024];
    let bound_response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: request_envelope(action.clone()),
            batch_version: 1,
            entries: vec![
                BatchPrepareEntry {
                    resource_key: action.resource_key.clone(),
                    old_locator: Some(Locator::from("primary")),
                    new_locator: Some(Locator::from("primary")),
                    expected_revision: Some("primary-revision".into()),
                    existence: ResourceExistence::Present,
                    resource_type: ResourceType::File,
                    metadata: ResourceMetadata {
                        size: Some(staged.len() as u64),
                        ..ResourceMetadata::default()
                    },
                    content: None,
                    staged_upload_id: Some(bound_upload_id.clone()),
                    fingerprint: sha256_fingerprint(&staged),
                    coverage: CaptureManifest {
                        byte_len: Some(staged.len() as u64),
                        ..CaptureManifest::default()
                    },
                },
                BatchPrepareEntry {
                    resource_key: action.modified_resource_ids[0].clone(),
                    old_locator: Some(Locator::from("secondary")),
                    new_locator: Some(Locator::from("secondary")),
                    expected_revision: Some("secondary-revision".into()),
                    existence: ResourceExistence::Present,
                    resource_type: ResourceType::File,
                    metadata: ResourceMetadata {
                        size: Some(oversized_inline.len() as u64),
                        ..ResourceMetadata::default()
                    },
                    content: Some(oversized_inline),
                    staged_upload_id: None,
                    fingerprint: "secondary-fingerprint".into(),
                    coverage: CaptureManifest {
                        byte_len: Some(513 * 1024),
                        ..CaptureManifest::default()
                    },
                },
            ],
        },
    )
    .await;
    assert!(matches!(bound_response, ServiceResponse::Error { .. }));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-service-cleanup"),
                upload_id: bound_upload_id.clone(),
                content_length: staged.len() as u64,
                fingerprint: sha256_fingerprint(&staged),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("batch-service-cleanup"),
                upload_id: bound_upload_id,
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    let denied_content = b"denied-stage".to_vec();
    let denied_owned_id = "denied-owned-stage".to_owned();
    let denied_other_id = "denied-other-stage".to_owned();
    for (stage_action, stage_id) in [
        ("batch-denied", denied_owned_id.clone()),
        ("other-action", denied_other_id.clone()),
    ] {
        assert!(matches!(
            send(
                &mut stream,
                ServiceRequest::StageBegin {
                    envelope: control(stage_action),
                    upload_id: stage_id.clone(),
                    content_length: denied_content.len() as u64,
                    fingerprint: sha256_fingerprint(&denied_content),
                },
            )
            .await,
            ServiceResponse::Accepted
        ));
        assert!(matches!(
            send(
                &mut stream,
                ServiceRequest::StageChunk {
                    envelope: control(stage_action),
                    upload_id: stage_id.clone(),
                    offset: 0,
                    content: Some(denied_content.clone()),
                },
            )
            .await,
            ServiceResponse::Accepted
        ));
        assert!(matches!(
            send(
                &mut stream,
                ServiceRequest::StageFinish {
                    envelope: control(stage_action),
                    upload_id: stage_id,
                },
            )
            .await,
            ServiceResponse::Staged { .. }
        ));
    }
    let mut denied = request();
    denied.action_id = "batch-denied".into();
    denied.modified_resource_ids = vec![ResourceKey {
        account_id: "other-account".into(),
        workspace_id: "workspace".into(),
        provider: "files".into(),
        resource_id: "foreign-resource".into(),
    }];
    let denied_entry = |resource_key: ResourceKey, upload_id: String| BatchPrepareEntry {
        resource_key,
        old_locator: Some(Locator::from("denied")),
        new_locator: Some(Locator::from("denied")),
        expected_revision: Some("denied-revision".into()),
        existence: ResourceExistence::Present,
        resource_type: ResourceType::File,
        metadata: ResourceMetadata {
            size: Some(denied_content.len() as u64),
            ..ResourceMetadata::default()
        },
        content: None,
        staged_upload_id: Some(upload_id),
        fingerprint: sha256_fingerprint(&denied_content),
        coverage: CaptureManifest {
            byte_len: Some(denied_content.len() as u64),
            ..CaptureManifest::default()
        },
    };
    let denied_response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: request_envelope(denied.clone()),
            batch_version: 1,
            entries: vec![
                denied_entry(denied.resource_key.clone(), denied_owned_id.clone()),
                denied_entry(
                    denied.modified_resource_ids[0].clone(),
                    denied_other_id.clone(),
                ),
            ],
        },
    )
    .await;
    assert!(matches!(denied_response, ServiceResponse::Error { .. }));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-denied"),
                upload_id: denied_owned_id.clone(),
                content_length: denied_content.len() as u64,
                fingerprint: sha256_fingerprint(&denied_content),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("batch-denied"),
                upload_id: denied_owned_id,
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("other-action"),
                upload_id: denied_other_id.clone(),
                content_length: denied_content.len() as u64,
                fingerprint: sha256_fingerprint(&denied_content),
            },
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("other-action"),
                upload_id: denied_other_id,
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    let bad_digest_content = b"bad-digest-stage";
    let bad_digest_id = "bad-digest-stage";
    stage_small(
        &mut stream,
        "batch-bad-digest",
        bad_digest_id,
        bad_digest_content,
    )
    .await;
    let mut bad_digest = request();
    bad_digest.action_id = "batch-bad-digest".into();
    let mut bad_digest_envelope = request_envelope(bad_digest);
    bad_digest_envelope.claimed_digest = "forged-digest".into();
    let bad_digest_response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: bad_digest_envelope,
            batch_version: 1,
            entries: vec![denied_entry(
                request().resource_key,
                bad_digest_id.to_owned(),
            )],
        },
    )
    .await;
    assert!(matches!(bad_digest_response, ServiceResponse::Error { .. }));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-bad-digest"),
                upload_id: bad_digest_id.to_owned(),
                content_length: bad_digest_content.len() as u64,
                fingerprint: sha256_fingerprint(bad_digest_content),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("batch-bad-digest"),
                upload_id: bad_digest_id.to_owned(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    let invalid_token_content = b"invalid-token-stage";
    let invalid_token_id = "invalid-token-stage";
    stage_small(
        &mut stream,
        "batch-invalid-token",
        invalid_token_id,
        invalid_token_content,
    )
    .await;
    let mut invalid_token = request();
    invalid_token.action_id = "batch-invalid-token".into();
    let mut invalid_token_envelope = request_envelope(invalid_token);
    invalid_token_envelope.auth.token = "wrong-token".into();
    let invalid_token_response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: invalid_token_envelope,
            batch_version: 1,
            entries: vec![denied_entry(
                request().resource_key,
                invalid_token_id.to_owned(),
            )],
        },
    )
    .await;
    assert!(matches!(
        invalid_token_response,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-invalid-token"),
                upload_id: invalid_token_id.to_owned(),
                content_length: invalid_token_content.len() as u64,
                fingerprint: sha256_fingerprint(invalid_token_content),
            },
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("batch-invalid-token"),
                upload_id: invalid_token_id.to_owned(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    let unsupported_content = b"unsupported-batch-version";
    let unsupported_id = "unsupported-batch-version-stage";
    stage_small(
        &mut stream,
        "batch-unsupported-version",
        unsupported_id,
        unsupported_content,
    )
    .await;
    let mut unsupported = request();
    unsupported.action_id = "batch-unsupported-version".into();
    let unsupported_response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: request_envelope(unsupported),
            batch_version: 2,
            entries: vec![denied_entry(
                request().resource_key,
                unsupported_id.to_owned(),
            )],
        },
    )
    .await;
    assert!(matches!(
        unsupported_response,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-unsupported-version"),
                upload_id: unsupported_id.to_owned(),
                content_length: unsupported_content.len() as u64,
                fingerprint: sha256_fingerprint(unsupported_content),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("batch-unsupported-version"),
                upload_id: unsupported_id.to_owned(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    let wrong_fingerprint_content = b"wrong-fingerprint-stage";
    let wrong_fingerprint_id = "wrong-fingerprint-stage";
    stage_small(
        &mut stream,
        "batch-wrong-fingerprint",
        wrong_fingerprint_id,
        wrong_fingerprint_content,
    )
    .await;
    let mut wrong_fingerprint = request();
    wrong_fingerprint.action_id = "batch-wrong-fingerprint".into();
    let mut wrong_entry = denied_entry(request().resource_key, wrong_fingerprint_id.to_owned());
    wrong_entry.fingerprint =
        "sha256:0000000000000000000000000000000000000000000000000000000000000000:23".into();
    let wrong_fingerprint_response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: request_envelope(wrong_fingerprint),
            batch_version: 1,
            entries: vec![wrong_entry],
        },
    )
    .await;
    assert!(matches!(
        wrong_fingerprint_response,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-wrong-fingerprint"),
                upload_id: wrong_fingerprint_id.to_owned(),
                content_length: wrong_fingerprint_content.len() as u64,
                fingerprint: sha256_fingerprint(wrong_fingerprint_content),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("batch-wrong-fingerprint"),
                upload_id: wrong_fingerprint_id.to_owned(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    let duplicate_content = b"duplicate-stage-reference";
    let duplicate_id = "duplicate-stage-reference";
    stage_small(
        &mut stream,
        "batch-duplicate-stage",
        duplicate_id,
        duplicate_content,
    )
    .await;
    let mut duplicate = request();
    duplicate.action_id = "batch-duplicate-stage".into();
    let duplicate_response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: request_envelope(duplicate),
            batch_version: 1,
            entries: vec![
                denied_entry(request().resource_key.clone(), duplicate_id.to_owned()),
                denied_entry(request().resource_key, duplicate_id.to_owned()),
            ],
        },
    )
    .await;
    assert!(matches!(duplicate_response, ServiceResponse::Error { .. }));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("batch-duplicate-stage"),
                upload_id: duplicate_id.to_owned(),
                content_length: duplicate_content.len() as u64,
                fingerprint: sha256_fingerprint(duplicate_content),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("batch-duplicate-stage"),
                upload_id: duplicate_id.to_owned(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    let mut omitted = request();
    omitted.action_id = "batch-service-omitted".into();
    omitted.modified_resource_ids = vec![ResourceKey {
        account_id: "account".into(),
        workspace_id: "workspace".into(),
        provider: "files".into(),
        resource_id: "omitted-secondary".into(),
    }];
    let omitted_entry = BatchPrepareEntry {
        resource_key: omitted.resource_key.clone(),
        old_locator: Some(Locator::from("primary")),
        new_locator: Some(Locator::from("primary")),
        expected_revision: Some("primary-revision".into()),
        existence: ResourceExistence::Present,
        resource_type: ResourceType::File,
        metadata: ResourceMetadata {
            size: Some(6),
            ..ResourceMetadata::default()
        },
        content: Some(b"before".to_vec()),
        staged_upload_id: None,
        fingerprint: "primary-fingerprint".into(),
        coverage: CaptureManifest {
            byte_len: Some(6),
            ..CaptureManifest::default()
        },
    };
    let response = send(
        &mut stream,
        ServiceRequest::PrepareBatch {
            envelope: request_envelope(omitted.clone()),
            batch_version: 1,
            entries: vec![omitted_entry],
        },
    )
    .await;
    match response {
        ServiceResponse::Error { code } => assert!(code.contains("missing_resource"), "{code}"),
        other => panic!("unexpected omitted-resource response: {other:?}"),
    }
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Abort {
                envelope: control(&omitted.action_id),
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control("batch-service-cleanup")),
        )
        .await,
        ServiceResponse::Accepted
    ));
    let _ = child.wait();
}

#[tokio::test]
async fn staged_admission_rejects_bad_fingerprints_and_reaps_abandoned_uploads() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("staged-admission.sock");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
            "account",
            dir.path().to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .env("OPENCLANK_HISTORY_STAGING_QUOTA_BYTES", "128")
        .env("OPENCLANK_HISTORY_STAGING_TTL_MS", "500")
        .env("OPENCLANK_HISTORY_MAX_ACTIVE_STAGES", "2")
        .spawn()
        .unwrap();
    let mut stream = connect(&socket).await;
    let malformed = send(
        &mut stream,
        ServiceRequest::StageBegin {
            envelope: control("bad-fingerprint"),
            upload_id: "bad-fingerprint".into(),
            content_length: 4,
            fingerprint: format!("sha256:{}:4", "A".repeat(64)),
        },
    )
    .await;
    assert!(matches!(malformed, ServiceResponse::Error { .. }));
    let mismatch = send(
        &mut stream,
        ServiceRequest::StageBegin {
            envelope: control("bad-length"),
            upload_id: "bad-length".into(),
            content_length: 4,
            fingerprint: format!("sha256:{}:3", "0".repeat(64)),
        },
    )
    .await;
    assert!(matches!(mismatch, ServiceResponse::Error { .. }));

    let first = b"first-stage".to_vec();
    let second = b"second-stage".to_vec();
    for (upload_id, content) in [("first", &first), ("second", &second)] {
        assert!(matches!(
            send(
                &mut stream,
                ServiceRequest::StageBegin {
                    envelope: control(upload_id),
                    upload_id: upload_id.into(),
                    content_length: content.len() as u64,
                    fingerprint: sha256_fingerprint(content),
                },
            )
            .await,
            ServiceResponse::Accepted
        ));
    }
    let usage = match send(&mut stream, ServiceRequest::GetUsage(control("usage"))).await {
        ServiceResponse::Usage(usage) => usage,
        other => panic!("unexpected usage response: {other:?}"),
    };
    assert_eq!(
        usage.reserved_inflight_bytes,
        (first.len() + second.len()) as u64
    );
    let mut limited = match send(&mut stream, ServiceRequest::GetPolicy(control("policy"))).await {
        ServiceResponse::Policy(policy) => policy,
        other => panic!("unexpected policy response: {other:?}"),
    };
    limited.global.total_bytes = usage.reserved_inflight_bytes + 1;
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::SetPolicy {
                envelope: control("policy"),
                expected_revision: limited.revision,
                policy: limited,
            },
        )
        .await,
        ServiceResponse::Policy(_)
    ));
    let over_global = send(
        &mut stream,
        ServiceRequest::StageBegin {
            envelope: control("over-global"),
            upload_id: "over-global".into(),
            content_length: 2,
            fingerprint: format!("sha256:{}:2", "0".repeat(64)),
        },
    )
    .await;
    assert!(matches!(over_global, ServiceResponse::Error { .. }));
    let mut restored_policy = match send(
        &mut stream,
        ServiceRequest::GetPolicy(control("policy-restore")),
    )
    .await
    {
        ServiceResponse::Policy(policy) => policy,
        other => panic!("unexpected policy response: {other:?}"),
    };
    restored_policy.global.total_bytes = 1_073_741_824;
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::SetPolicy {
                envelope: control("policy-restore"),
                expected_revision: restored_policy.revision,
                policy: restored_policy,
            },
        )
        .await,
        ServiceResponse::Policy(_)
    ));
    let full = send(
        &mut stream,
        ServiceRequest::StageBegin {
            envelope: control("full"),
            upload_id: "full".into(),
            content_length: 1,
            fingerprint: sha256_fingerprint(b"x"),
        },
    )
    .await;
    assert!(matches!(full, ServiceResponse::Error { .. }));
    let over_quota = send(
        &mut stream,
        ServiceRequest::StageBegin {
            envelope: control("over-quota"),
            upload_id: "over-quota".into(),
            content_length: 129,
            fingerprint: format!("sha256:{}:129", "0".repeat(64)),
        },
    )
    .await;
    assert!(matches!(over_quota, ServiceResponse::Error { .. }));

    let corrupt = b"good";
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("corrupt"),
                upload_id: "corrupt".into(),
                content_length: corrupt.len() as u64,
                fingerprint: sha256_fingerprint(corrupt),
            },
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("first"),
                upload_id: "first".into(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("corrupt"),
                upload_id: "corrupt".into(),
                content_length: corrupt.len() as u64,
                fingerprint: sha256_fingerprint(corrupt),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageChunk {
                envelope: control("corrupt"),
                upload_id: "corrupt".into(),
                offset: 0,
                content: Some(b"evil".to_vec()),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    let corrupt_finish = send(
        &mut stream,
        ServiceRequest::StageFinish {
            envelope: control("corrupt"),
            upload_id: "corrupt".into(),
        },
    )
    .await;
    assert!(matches!(corrupt_finish, ServiceResponse::Error { .. }));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("corrupt"),
                upload_id: "corrupt".into(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));

    tokio::time::sleep(std::time::Duration::from_millis(600)).await;
    let replacement = b"renewed";
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageBegin {
                envelope: control("replacement"),
                upload_id: "replacement".into(),
                content_length: replacement.len() as u64,
                fingerprint: sha256_fingerprint(replacement),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    let status = match send(&mut stream, ServiceRequest::GetStatus(control("status"))).await {
        ServiceResponse::Status { staged_bytes, .. } => staged_bytes,
        other => panic!("unexpected status response: {other:?}"),
    };
    assert_eq!(status, replacement.len() as u64);
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::StageAbort {
                envelope: control("replacement"),
                upload_id: "replacement".into(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    child.kill().unwrap();
    let _ = child.wait();
}

#[cfg(feature = "test_faults")]
#[tokio::test]
async fn prepare_read_abort_and_upload_id_reuse_preserve_replacement() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("staged-reuse-race.sock");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
            "account",
            dir.path().to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .env("OPENCLANK_HISTORY_TEST_STAGE_IO_DELAY_MS", "500")
        .spawn()
        .unwrap();
    let action_id = "staged-reuse-race";
    let old_content = b"old-stage-content".to_vec();
    let mut staging = connect(&socket).await;
    stage_small(&mut staging, action_id, "U", &old_content).await;

    let mut action = request();
    action.action_id = action_id.into();
    let entry = BatchPrepareEntry {
        resource_key: action.resource_key.clone(),
        old_locator: Some(Locator::from("resource")),
        new_locator: Some(Locator::from("resource")),
        expected_revision: Some("r1".into()),
        existence: ResourceExistence::Present,
        resource_type: ResourceType::File,
        metadata: ResourceMetadata {
            size: Some(old_content.len() as u64),
            ..ResourceMetadata::default()
        },
        content: None,
        staged_upload_id: Some("U".into()),
        fingerprint: sha256_fingerprint(&old_content),
        coverage: CaptureManifest {
            byte_len: Some(old_content.len() as u64),
            ..CaptureManifest::default()
        },
    };
    let envelope = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: String::new(),
        request: action,
    };
    let mut prepare_stream = connect(&socket).await;
    let prepare_task = tokio::spawn(async move {
        send(
            &mut prepare_stream,
            ServiceRequest::PrepareBatch {
                envelope,
                batch_version: 1,
                entries: vec![entry],
            },
        )
        .await
    });
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;

    // StageAbort arrives over another connection while PrepareBatch holds the
    // captured stage lock. Its completion removes only the old instance.
    let mut abort_stream = connect(&socket).await;
    let abort_task = tokio::spawn(async move {
        send(
            &mut abort_stream,
            ServiceRequest::StageAbort {
                envelope: control(action_id),
                upload_id: "U".into(),
            },
        )
        .await
    });
    let abort_response = tokio::time::timeout(std::time::Duration::from_secs(3), abort_task)
        .await
        .expect("StageAbort remained blocked behind the staged read")
        .unwrap();
    assert!(matches!(abort_response, ServiceResponse::Accepted));

    // Reuse U before the old PrepareBatch has returned. Its cleanup is queued
    // on the captured lock and must not remove this replacement instance.
    let replacement = b"replacement-stage".to_vec();
    let mut reuse = connect(&socket).await;
    assert!(matches!(
        send(
            &mut reuse,
            ServiceRequest::StageBegin {
                envelope: control(action_id),
                upload_id: "U".into(),
                content_length: replacement.len() as u64,
                fingerprint: sha256_fingerprint(&replacement),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    let prepare_response = tokio::time::timeout(std::time::Duration::from_secs(3), prepare_task)
        .await
        .expect("PrepareBatch did not finish")
        .unwrap();
    assert!(matches!(prepare_response, ServiceResponse::Action(_)));
    assert!(matches!(
        send(
            &mut reuse,
            ServiceRequest::StageChunk {
                envelope: control(action_id),
                upload_id: "U".into(),
                offset: 0,
                content: Some(replacement.clone()),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        send(
            &mut reuse,
            ServiceRequest::StageFinish {
                envelope: control(action_id),
                upload_id: "U".into(),
            },
        )
        .await,
        ServiceResponse::Staged { .. }
    ));
    assert!(matches!(
        send(
            &mut reuse,
            ServiceRequest::StageAbort {
                envelope: control(action_id),
                upload_id: "U".into(),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    child.kill().unwrap();
    let _ = child.wait();
}

#[tokio::test]
async fn staged_admission_serializes_reservations_and_keeps_health_responsive() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("staged-concurrent.sock");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
            "account",
            dir.path().to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .env("OPENCLANK_HISTORY_STAGING_QUOTA_BYTES", "200")
        .spawn()
        .unwrap();
    let mut left = connect(&socket).await;
    let mut right = connect(&socket).await;
    let left_content = vec![0x11; 128];
    let right_content = vec![0x22; 128];
    let (left_begin, right_begin) = tokio::join!(
        send(
            &mut left,
            ServiceRequest::StageBegin {
                envelope: control("left"),
                upload_id: "left".into(),
                content_length: left_content.len() as u64,
                fingerprint: sha256_fingerprint(&left_content),
            }
        ),
        send(
            &mut right,
            ServiceRequest::StageBegin {
                envelope: control("right"),
                upload_id: "right".into(),
                content_length: right_content.len() as u64,
                fingerprint: sha256_fingerprint(&right_content),
            }
        )
    );
    assert_eq!(
        [
            matches!(left_begin, ServiceResponse::Accepted),
            matches!(right_begin, ServiceResponse::Accepted)
        ]
        .into_iter()
        .filter(|accepted| *accepted)
        .count(),
        1
    );
    let mut health = connect(&socket).await;
    let health_response = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        send(&mut health, ServiceRequest::Health(control("health"))),
    )
    .await
    .expect("Health was blocked by a staged upload");
    assert!(matches!(health_response, ServiceResponse::Health { .. }));
    let mut usage_stream = connect(&socket).await;
    let usage = match send(
        &mut usage_stream,
        ServiceRequest::GetUsage(control("usage-concurrent")),
    )
    .await
    {
        ServiceResponse::Usage(usage) => usage,
        other => panic!("unexpected usage response: {other:?}"),
    };
    assert!(usage.reserved_inflight_bytes <= 200);
    for (stream, upload_id) in [(&mut left, "left"), (&mut right, "right")] {
        let _ = send(
            stream,
            ServiceRequest::StageAbort {
                envelope: control(upload_id),
                upload_id: upload_id.into(),
            },
        )
        .await;
    }
    child.kill().unwrap();
    let _ = child.wait();
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn staged_slow_io_is_not_reaped_and_expiry_frees_staging_admission() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("staged-slow.sock");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
            "account",
            dir.path().to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .env("OPENCLANK_HISTORY_STAGING_QUOTA_BYTES", "256")
        .env("OPENCLANK_HISTORY_STAGING_TTL_MS", "200")
        .env("OPENCLANK_HISTORY_MAX_ACTIVE_STAGES", "3")
        .env("OPENCLANK_HISTORY_TEST_STAGE_IO_DELAY_MS", "300")
        .spawn()
        .unwrap();
    let mut abandoned = connect(&socket).await;
    let mut left = connect(&socket).await;
    let mut right = connect(&socket).await;
    let abandoned_content = vec![0x01; 32];
    let left_content = vec![0x02; 96];
    let right_content = vec![0x03; 96];
    for (stream, upload_id, content) in [
        (&mut abandoned, "abandoned", &abandoned_content),
        (&mut left, "slow-left", &left_content),
        (&mut right, "slow-right", &right_content),
    ] {
        assert!(matches!(
            send(
                stream,
                ServiceRequest::StageBegin {
                    envelope: control(upload_id),
                    upload_id: upload_id.into(),
                    content_length: content.len() as u64,
                    fingerprint: sha256_fingerprint(content),
                },
            )
            .await,
            ServiceResponse::Accepted
        ));
    }
    let left_task = tokio::spawn(async move {
        send(
            &mut left,
            ServiceRequest::StageChunk {
                envelope: control("slow-left"),
                upload_id: "slow-left".into(),
                offset: 0,
                content: Some(left_content),
            },
        )
        .await
    });
    let right_task = tokio::spawn(async move {
        send(
            &mut right,
            ServiceRequest::StageChunk {
                envelope: control("slow-right"),
                upload_id: "slow-right".into(),
                offset: 0,
                content: Some(right_content),
            },
        )
        .await
    });
    let mut left_waiter = connect(&socket).await;
    let left_waiter_task = tokio::spawn(async move {
        send(
            &mut left_waiter,
            ServiceRequest::StageChunk {
                envelope: control("slow-left"),
                upload_id: "slow-left".into(),
                offset: 0,
                content: Some(vec![0x04; 96]),
            },
        )
        .await
    });
    tokio::time::sleep(std::time::Duration::from_millis(250)).await;
    let mut replacement = connect(&socket).await;
    let replacement_response = send(
        &mut replacement,
        ServiceRequest::StageBegin {
            envelope: control("replacement"),
            upload_id: "replacement".into(),
            content_length: abandoned_content.len() as u64,
            fingerprint: sha256_fingerprint(&abandoned_content),
        },
    )
    .await;
    assert!(
        matches!(replacement_response, ServiceResponse::Accepted),
        "replacement response: {replacement_response:?}"
    );
    let mut replacement_io = connect(&socket).await;
    let replacement_chunk_task = tokio::spawn(async move {
        tokio::time::timeout(
            std::time::Duration::from_millis(550),
            send(
                &mut replacement_io,
                ServiceRequest::StageChunk {
                    envelope: control("replacement"),
                    upload_id: "replacement".into(),
                    offset: 0,
                    content: Some(abandoned_content.clone()),
                },
            ),
        )
        .await
        .expect("a slow hash blocked unrelated staging I/O")
    });
    assert!(matches!(
        left_task.await.unwrap(),
        ServiceResponse::Accepted
    ));
    let mut finish_stream = connect(&socket).await;
    let finish_task = tokio::spawn(async move {
        send(
            &mut finish_stream,
            ServiceRequest::StageFinish {
                envelope: control("slow-left"),
                upload_id: "slow-left".into(),
            },
        )
        .await
    });
    assert!(matches!(
        right_task.await.unwrap(),
        ServiceResponse::Accepted
    ));
    assert!(matches!(
        left_waiter_task.await.unwrap(),
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        replacement_chunk_task.await.unwrap(),
        ServiceResponse::Accepted
    ));
    let finish_response = finish_task.await.unwrap();
    assert!(
        matches!(&finish_response, ServiceResponse::Staged { .. }),
        "finish response: {finish_response:?}"
    );
    for (stream, upload_id) in [
        (&mut abandoned, "abandoned"),
        (&mut replacement, "replacement"),
    ] {
        let _ = send(
            stream,
            ServiceRequest::StageAbort {
                envelope: control(upload_id),
                upload_id: upload_id.into(),
            },
        )
        .await;
    }
    child.kill().unwrap();
    let _ = child.wait();
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn cancelled_staged_io_keeps_reservation_until_blocking_write_finishes() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("staged-cancel.sock");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
            "account",
            dir.path().to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .env("OPENCLANK_HISTORY_STAGING_QUOTA_BYTES", "32")
        .env("OPENCLANK_HISTORY_STAGING_TTL_MS", "200")
        .env("OPENCLANK_HISTORY_MAX_ACTIVE_STAGES", "1")
        .env("OPENCLANK_HISTORY_TEST_STAGE_IO_DELAY_MS", "1000")
        .spawn()
        .unwrap();
    let mut cancelled = connect(&socket).await;
    let payload = vec![0x05; 32];
    assert!(matches!(
        send(
            &mut cancelled,
            ServiceRequest::StageBegin {
                envelope: control("cancelled"),
                upload_id: "cancelled".into(),
                content_length: payload.len() as u64,
                fingerprint: sha256_fingerprint(&payload),
            },
        )
        .await,
        ServiceResponse::Accepted
    ));
    let mut cancelled_io = connect(&socket).await;
    let cancelled_task = tokio::spawn(async move {
        send(
            &mut cancelled_io,
            ServiceRequest::StageChunk {
                envelope: control("cancelled"),
                upload_id: "cancelled".into(),
                offset: 0,
                content: Some(payload),
            },
        )
        .await
    });
    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
    cancelled_task.abort();

    let mut replacement = connect(&socket).await;
    let replacement_begin = send(
        &mut replacement,
        ServiceRequest::StageBegin {
            envelope: control("replacement-before-completion"),
            upload_id: "replacement-before-completion".into(),
            content_length: 32,
            fingerprint: sha256_fingerprint(&[0x06; 32]),
        },
    )
    .await;
    assert!(matches!(replacement_begin, ServiceResponse::Error { .. }));

    tokio::time::sleep(std::time::Duration::from_millis(900)).await;
    let replacement_begin = send(
        &mut replacement,
        ServiceRequest::StageBegin {
            envelope: control("replacement-after-completion"),
            upload_id: "replacement-after-completion".into(),
            content_length: 32,
            fingerprint: sha256_fingerprint(&[0x06; 32]),
        },
    )
    .await;
    assert!(matches!(replacement_begin, ServiceResponse::Accepted));
    let _ = send(
        &mut replacement,
        ServiceRequest::StageAbort {
            envelope: control("replacement-after-completion"),
            upload_id: "replacement-after-completion".into(),
        },
    )
    .await;
    child.kill().unwrap();
    let _ = child.wait();
}

#[tokio::test]
async fn subprocess_service_prepare_live_complete_read_and_shutdown() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("history.sock");
    let receipt_root = dir.path().join("restore-receipts");
    let resource_map = receipt_root.join("host-resources.json");
    std::fs::create_dir_all(&receipt_root).unwrap();
    std::fs::write(
        &resource_map,
        serde_json::json!({
            "version": 1,
            "entries": {
                "file:test-resource": {
                    "resource_id": "file:test-resource",
                    "account_id": "account",
                    "workspace_id": "workspace",
                    "root_id": "host-root",
                    "root_path": dir.path().to_string_lossy(),
                    "relative_path": "restored-note.md",
                    "active": true,
                    "generation": 1,
                    "updated_millis": 0
                }
            }
        })
        .to_string(),
    )
    .unwrap();
    let child_bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(child_bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
            "account",
            dir.path().to_str().unwrap(),
            receipt_root.to_str().unwrap(),
            resource_map.to_str().unwrap(),
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .env("OPENCLANK_HISTORY_OWNER_GRANTS", "actor:owner-b")
        .spawn()
        .unwrap();
    let mut stream = loop {
        match UnixStream::connect(&socket).await {
            Ok(stream) => break stream.into_split(),
            Err(_) => {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        }
    };
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        std::fs::metadata(&socket).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let mut action = request();
    action.resource_key.resource_id = "file:test-resource".into();
    let prepared = send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: RequestEnvelope {
                protocol_version: PROTOCOL_VERSION,
                auth: AuthContext {
                    actor_id: "actor".into(),
                    account_id: "account".into(),
                    token: "token".into(),
                },
                // Cross-language clients may omit the digest; the service computes and binds it
                // from the decoded Rust request before beginning the action.
                claimed_digest: String::new(),
                request: action.clone(),
            },
            content: Some(b"before".to_vec()),
            fingerprint: "before".into(),
        },
    )
    .await;
    assert!(matches!(
        prepared,
        ServiceResponse::Action(ActionRecord { .. })
    ));
    let prepared_retry = send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: RequestEnvelope {
                protocol_version: PROTOCOL_VERSION,
                auth: AuthContext {
                    actor_id: "actor".into(),
                    account_id: "account".into(),
                    token: "token".into(),
                },
                claimed_digest: request_digest(&action),
                request: action.clone(),
            },
            content: Some(b"before".to_vec()),
            fingerprint: "before".into(),
        },
    )
    .await;
    assert!(matches!(
        prepared_retry,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::BeforeDurable,
            ..
        })
    ));
    let prepared_conflict = send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: RequestEnvelope {
                protocol_version: PROTOCOL_VERSION,
                auth: AuthContext {
                    actor_id: "actor".into(),
                    account_id: "account".into(),
                    token: "token".into(),
                },
                claimed_digest: request_digest(&action),
                request: action.clone(),
            },
            content: Some(b"different".to_vec()),
            fingerprint: "before".into(),
        },
    )
    .await;
    assert!(matches!(prepared_conflict, ServiceResponse::Error { .. }));
    let mut wrong_actor = control(&action.action_id);
    wrong_actor.auth.actor_id = "other-actor".into();
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::RecordLive {
                envelope: wrong_actor,
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::Committed,
                    fingerprint: Some("live".into())
                }
            }
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    let guarded_other_owner = ActionRequest {
        action_id: "guarded-other-owner".into(),
        guard_resource_ids: vec![openclank_history::catalog::ResourceKey {
            account_id: "owner-c".into(),
            workspace_id: "workspace".into(),
            provider: "files".into(),
            resource_id: "guard".into(),
        }],
        ..request()
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: RequestEnvelope {
                    protocol_version: PROTOCOL_VERSION,
                    auth: AuthContext {
                        actor_id: "actor".into(),
                        account_id: "account".into(),
                        token: "token".into(),
                    },
                    claimed_digest: request_digest(&guarded_other_owner),
                    request: guarded_other_owner,
                },
                content: Some(b"guarded".to_vec()),
                fingerprint: "guarded".into(),
            }
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    let mut wrong_account = control(&action.action_id);
    wrong_account.auth.account_id = "other-account".into();
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::ReadVersion {
                envelope: wrong_account,
                receipt: VersionReceipt {
                    version_id: "missing".into(),
                    content: VersionContent::Empty,
                    fingerprint: "missing".into()
                }
            }
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    let mut wrong_token = control(&action.action_id);
    wrong_token.auth.token = "wrong-token".into();
    assert!(matches!(
        send(&mut stream, ServiceRequest::Health(wrong_token)).await,
        ServiceResponse::Error { .. }
    ));
    let mut wrong_protocol = control(&action.action_id);
    wrong_protocol.protocol_version = PROTOCOL_VERSION + 1;
    assert!(matches!(
        send(&mut stream, ServiceRequest::Health(wrong_protocol)).await,
        ServiceResponse::Error { .. }
    ));
    let live = send(
        &mut stream,
        ServiceRequest::RecordLive {
            envelope: control(&action.action_id),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::Committed,
                fingerprint: Some("live".into()),
            },
        },
    )
    .await;
    assert!(matches!(live, ServiceResponse::Action(_)));
    let live_retry = send(
        &mut stream,
        ServiceRequest::RecordLive {
            envelope: control(&action.action_id),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::Committed,
                fingerprint: Some("live".into()),
            },
        },
    )
    .await;
    assert!(matches!(
        live_retry,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Applied,
            ..
        })
    ));
    let completed = send(
        &mut stream,
        ServiceRequest::Complete {
            envelope: control(&action.action_id),
            content: Some(b"after".to_vec()),
            fingerprint: "after".into(),
        },
    )
    .await;
    let after = match completed {
        ServiceResponse::Action(record) => record.after.unwrap(),
        other => panic!("unexpected response: {other:?}"),
    };
    let destination_path = dir.path().join("restored-note.md");
    let restored = send(
        &mut stream,
        ServiceRequest::RestoreHost {
            envelope: control("restore-service"),
            request: RestoreRequest {
                restore_id: "restore-service".into(),
                account_id: "account".into(),
                source_action_id: action.action_id.clone(),
                source_version_id: after.version_id.clone(),
                destination: action.resource_key.clone(),
                expected_destination_fingerprint: None,
                require_current_capture: true,
            },
            source: after.clone(),
            destination_path: destination_path.to_string_lossy().into_owned(),
            source_host_metadata: None,
        },
    )
    .await;
    match restored {
        ServiceResponse::Restore(receipt) => assert_eq!(receipt.outcome, RestoreOutcome::Complete),
        other => panic!("unexpected restore response: {other:?}"),
    }
    assert_eq!(std::fs::read(&destination_path).unwrap(), b"after");
    let expected_after_restore = FilesystemRestoreProvider::new_with_receipt_root(
        &destination_path,
        dir.path().join("restore-fingerprint-receipts"),
    )
    .current_fingerprint()
    .unwrap();
    std::fs::write(&destination_path, b"unrelated later write").unwrap();
    let guarded_conflict = send(
        &mut stream,
        ServiceRequest::RestoreHost {
            envelope: control("restore-later-write-conflict"),
            request: RestoreRequest {
                restore_id: "restore-later-write-conflict".into(),
                account_id: "account".into(),
                source_action_id: action.action_id.clone(),
                source_version_id: after.version_id.clone(),
                destination: action.resource_key.clone(),
                expected_destination_fingerprint: expected_after_restore,
                require_current_capture: true,
            },
            source: after.clone(),
            destination_path: destination_path.to_string_lossy().into_owned(),
            source_host_metadata: None,
        },
    )
    .await;
    assert!(
        matches!(guarded_conflict, ServiceResponse::Error { ref code } if code.contains("Conflict")),
        "unexpected guarded restore response: {guarded_conflict:?}"
    );
    assert_eq!(
        std::fs::read(&destination_path).unwrap(),
        b"unrelated later write"
    );
    let tampered_path = dir.path().join("tampered-note.md");
    let tampered = send(
        &mut stream,
        ServiceRequest::RestoreHost {
            envelope: control("restore-tampered"),
            request: RestoreRequest {
                restore_id: "restore-tampered".into(),
                account_id: "account".into(),
                source_action_id: action.action_id.clone(),
                source_version_id: after.version_id.clone(),
                destination: action.resource_key.clone(),
                expected_destination_fingerprint: None,
                require_current_capture: true,
            },
            source: after.clone(),
            destination_path: tampered_path.to_string_lossy().into_owned(),
            source_host_metadata: None,
        },
    )
    .await;
    assert!(matches!(tampered, ServiceResponse::Error { .. }));
    assert!(!tampered_path.exists());
    let read = send(
        &mut stream,
        ServiceRequest::ReadVersion {
            envelope: control(&action.action_id),
            receipt: after.clone(),
        },
    )
    .await;
    assert_eq!(read, ServiceResponse::Bytes(Some(b"after".to_vec())));
    let complete_retry = send(
        &mut stream,
        ServiceRequest::Complete {
            envelope: control(&action.action_id),
            content: Some(b"after".to_vec()),
            fingerprint: "after".into(),
        },
    )
    .await;
    assert!(matches!(
        complete_retry,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Complete,
            ..
        })
    ));
    let complete_conflict = send(
        &mut stream,
        ServiceRequest::Complete {
            envelope: control(&action.action_id),
            content: Some(b"different".to_vec()),
            fingerprint: "after".into(),
        },
    )
    .await;
    assert!(matches!(complete_conflict, ServiceResponse::Error { .. }));
    let mut forged = after;
    forged.content = VersionContent::Bytes(LoreRef {
        context: [9; 16],
        hash_hex: "00".repeat(32),
    });
    let forged_read = send(
        &mut stream,
        ServiceRequest::ReadVersion {
            envelope: control(&action.action_id),
            receipt: forged,
        },
    )
    .await;
    assert!(matches!(forged_read, ServiceResponse::Error { .. }));
    let abort_action = ActionRequest {
        action_id: "abort-action".into(),
        ..request()
    };
    let abort_env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&abort_action),
        request: abort_action.clone(),
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: abort_env,
                content: Some(b"abort".to_vec()),
                fingerprint: "abort".into()
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Abort {
                envelope: control(&abort_action.action_id)
            }
        )
        .await,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    let active_action = ActionRequest {
        action_id: "active-shutdown".into(),
        ..request()
    };
    let active_env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&active_action),
        request: active_action.clone(),
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: active_env,
                content: Some(b"active".to_vec()),
                fingerprint: "active".into()
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Abort {
                envelope: control(&active_action.action_id)
            }
        )
        .await,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    for (action_id, status) in [
        ("conflict-action", LiveStatus::Conflict),
        ("partial-action", LiveStatus::Partial),
    ] {
        let outcome = ActionRequest {
            action_id: action_id.into(),
            ..request()
        };
        let envelope = RequestEnvelope {
            protocol_version: PROTOCOL_VERSION,
            auth: AuthContext {
                actor_id: "actor".into(),
                account_id: "account".into(),
                token: "token".into(),
            },
            claimed_digest: request_digest(&outcome),
            request: outcome.clone(),
        };
        assert!(matches!(
            send(
                &mut stream,
                ServiceRequest::Prepare {
                    envelope,
                    content: Some(b"outcome".to_vec()),
                    fingerprint: action_id.into()
                }
            )
            .await,
            ServiceResponse::Action(_)
        ));
        let result = send(
            &mut stream,
            ServiceRequest::RecordLive {
                envelope: control(action_id),
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action_id.into(),
                    status,
                    fingerprint: None,
                },
            },
        )
        .await;
        assert!(matches!(
            result,
            ServiceResponse::Action(ActionRecord {
                state: ActionState::Conflict | ActionState::Partial,
                ..
            })
        ));
    }
    let next = ActionRequest {
        action_id: "after-outcomes".into(),
        ..request()
    };
    let next_env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&next),
        request: next,
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: next_env,
                content: Some(b"next".to_vec()),
                fingerprint: "next".into()
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Abort {
                envelope: control("conflict-action")
            }
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Abort {
                envelope: control("partial-action")
            }
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Abort {
                envelope: control("after-outcomes")
            }
        )
        .await,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    let mut shared = ActionRequest {
        action_id: "shared-owner".into(),
        ..request()
    };
    shared.resource_key.account_id = "owner-b".into();
    let shared_env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&shared),
        request: shared.clone(),
    };
    let shared_record = match send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: shared_env,
            content: Some(b"owner bytes".to_vec()),
            fingerprint: "owner-fp".into(),
        },
    )
    .await
    {
        ServiceResponse::Action(record) => record,
        other => panic!("shared owner denied: {other:?}"),
    };
    let shared_before = shared_record.before.clone().unwrap();
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::ReadVersion {
                envelope: control("shared-owner"),
                receipt: shared_before
            }
        )
        .await,
        ServiceResponse::Bytes(Some(b"owner bytes".to_vec()))
    );
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Abort {
                envelope: control("shared-owner")
            }
        )
        .await,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    let mut denied = ActionRequest {
        action_id: "denied-owner".into(),
        ..request()
    };
    denied.resource_key.account_id = "owner-c".into();
    let denied_env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&denied),
        request: denied,
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: denied_env,
                content: Some(b"denied".to_vec()),
                fingerprint: "denied".into()
            }
        )
        .await,
        ServiceResponse::Error { .. }
    ));
    drop(stream);
    let mut malformed = UnixStream::connect(&socket).await.unwrap();
    malformed.write_all(b"not-json\n").await.unwrap();
    let mut malformed_reply = String::new();
    BufReader::new(malformed)
        .read_line(&mut malformed_reply)
        .await
        .unwrap();
    assert!(malformed_reply.contains("Error"));
    let mut oversized = UnixStream::connect(&socket).await.unwrap();
    oversized
        .write_all(&vec![b'x'; 1024 * 1024 + 1])
        .await
        .unwrap();
    oversized.write_all(b"\n").await.unwrap();
    drop(oversized);
    let mut shutdown_stream = UnixStream::connect(&socket).await.unwrap().into_split();
    let shutdown = send(
        &mut shutdown_stream,
        ServiceRequest::Shutdown(control(&action.action_id)),
    )
    .await;
    assert_eq!(shutdown, ServiceResponse::Accepted);
    assert!(child.wait().unwrap().success());
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn restore_service_reopens_after_each_durable_phase() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("restore-phases.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let receipts = dir.path().join("restore-receipts");
    let map = receipts.join("host-resources.json");
    let stages = [
        "prepared",
        "current_captured",
        "applying",
        "after_provider_commit",
    ];
    std::fs::create_dir_all(&receipts).unwrap();
    let mappings: serde_json::Map<String, serde_json::Value> = stages
        .iter()
        .map(|stage| {
            let resource_id = format!("file:target-{stage}");
            (
                resource_id.clone(),
                serde_json::json!({
                    "resource_id": resource_id,
                    "account_id": "account",
                    "workspace_id": "workspace",
                    "root_id": "host-root",
                    "root_path": dir.path().to_string_lossy(),
                    "relative_path": format!("target-{stage}.md"),
                    "active": true,
                    "generation": 1,
                    "updated_millis": 0
                }),
            )
        })
        .collect();
    std::fs::write(
        &map,
        serde_json::to_vec(&serde_json::json!({"version": 1, "entries": mappings})).unwrap(),
    )
    .unwrap();
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let spawn = |abort_stage: Option<&str>| {
        let mut command = std::process::Command::new(bin);
        command
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "account",
                dir.path().to_str().unwrap(),
                receipts.to_str().unwrap(),
                map.to_str().unwrap(),
            ])
            .env("OPENCLANK_HISTORY_TOKEN", "token");
        if let Some(stage) = abort_stage {
            command.env("OPENCLANK_HISTORY_RESTORE_ABORT_STAGE", stage);
        }
        command.spawn().unwrap()
    };
    let mut seed = spawn(None);
    let mut stream = connect(&socket).await;
    let mut action = request();
    action.action_id = "restore-phase-source".into();
    let auth = control(&action.action_id).auth;
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: RequestEnvelope {
                    protocol_version: PROTOCOL_VERSION,
                    auth,
                    claimed_digest: String::new(),
                    request: action.clone()
                },
                content: Some(b"before".to_vec()),
                fingerprint: "before".into(),
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::RecordLive {
                envelope: control(&action.action_id),
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::Committed,
                    fingerprint: Some("live".into())
                },
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    let source = match send(
        &mut stream,
        ServiceRequest::Complete {
            envelope: control(&action.action_id),
            content: Some(b"source".to_vec()),
            fingerprint: "source".into(),
        },
    )
    .await
    {
        ServiceResponse::Action(record) => record.after.unwrap(),
        other => panic!("source: {other:?}"),
    };
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Accepted
    );
    assert!(seed.wait().unwrap().success());
    drop(stream);

    for stage in stages {
        let restore_id = format!("restore-phase-{stage}");
        let resource_id = format!("file:target-{stage}");
        let destination = dir.path().join(format!("target-{stage}.md"));
        let restore_request = RestoreRequest {
            restore_id: restore_id.clone(),
            account_id: "account".into(),
            source_action_id: action.action_id.clone(),
            source_version_id: source.version_id.clone(),
            destination: openclank_history::catalog::ResourceKey {
                account_id: "account".into(),
                workspace_id: "workspace".into(),
                provider: "files".into(),
                resource_id,
            },
            expected_destination_fingerprint: None,
            require_current_capture: true,
        };
        let mut crashing = spawn(Some(stage));
        let mut crashing_stream = connect(&socket).await;
        let response = send_maybe(
            &mut crashing_stream,
            ServiceRequest::RestoreHost {
                envelope: control(&restore_id),
                request: restore_request.clone(),
                source: source.clone(),
                destination_path: destination.to_string_lossy().into_owned(),
                source_host_metadata: None,
            },
        )
        .await;
        assert!(
            response.is_none(),
            "abort stage {stage} unexpectedly returned: {response:?}"
        );
        let _ = crashing.kill();
        let _ = crashing.wait();
        drop(crashing_stream);
        let mut reopened_child = spawn(None);
        let mut reopened = connect(&socket).await;
        match send(
            &mut reopened,
            ServiceRequest::RestoreHost {
                envelope: control(&restore_id),
                request: restore_request,
                source: source.clone(),
                destination_path: destination.to_string_lossy().into_owned(),
                source_host_metadata: None,
            },
        )
        .await
        {
            ServiceResponse::Restore(receipt) => {
                assert_eq!(receipt.outcome, RestoreOutcome::Complete, "{stage}")
            }
            other => panic!("reopen {stage}: {other:?}"),
        }
        assert_eq!(std::fs::read(&destination).unwrap(), b"source");
        assert_eq!(
            send(
                &mut reopened,
                ServiceRequest::Shutdown(control(&restore_id))
            )
            .await,
            ServiceResponse::Accepted
        );
        assert!(reopened_child.wait().unwrap().success());
    }
}

#[tokio::test]
async fn child_kill_leaves_reopenable_catalog_and_lore() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("crash.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let spawn = || {
        std::process::Command::new(bin)
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "account",
            ])
            .env("OPENCLANK_HISTORY_TOKEN", "token")
            .spawn()
            .unwrap()
    };
    let mut first = spawn();
    let mut stream = loop {
        match UnixStream::connect(&socket).await {
            Ok(stream) => break stream.into_split(),
            Err(_) => {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        }
    };
    let action = ActionRequest {
        action_id: "crash-action".into(),
        ..request()
    };
    let response = send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: RequestEnvelope {
                protocol_version: PROTOCOL_VERSION,
                auth: AuthContext {
                    actor_id: "actor".into(),
                    account_id: "account".into(),
                    token: "token".into(),
                },
                claimed_digest: request_digest(&action),
                request: action.clone(),
            },
            content: Some(b"survive kill".to_vec()),
            fingerprint: "before".into(),
        },
    )
    .await;
    let before = match response {
        ServiceResponse::Action(record) => record.before.unwrap(),
        other => panic!("unexpected response: {other:?}"),
    };
    first.kill().unwrap();
    first.wait().unwrap();
    drop(stream);
    let mut second = spawn();
    let mut reopened = loop {
        match UnixStream::connect(&socket).await {
            Ok(stream) => break stream.into_split(),
            Err(_) => {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        }
    };
    let reconciled = send(
        &mut reopened,
        ServiceRequest::Reconcile {
            envelope: control(&action.action_id),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::NotCommitted,
                fingerprint: None,
            },
        },
    )
    .await;
    assert!(matches!(
        reconciled,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    let read = send(
        &mut reopened,
        ServiceRequest::ReadVersion {
            envelope: control(&action.action_id),
            receipt: before,
        },
    )
    .await;
    assert_eq!(read, ServiceResponse::Bytes(Some(b"survive kill".to_vec())));
    assert_eq!(
        send(
            &mut reopened,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Accepted
    );
    assert!(second.wait().unwrap().success());
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn child_abort_after_lore_flush_reopens_without_catalog_reference() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("flush-abort.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            catalog.to_str().unwrap(),
            lore.to_str().unwrap(),
            "account",
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .env("OPENCLANK_HISTORY_ABORT_STAGE", "after_lore_flush")
        .spawn()
        .unwrap();
    let mut stream = loop {
        match UnixStream::connect(&socket).await {
            Ok(stream) => break stream.into_split(),
            Err(_) => {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        }
    };
    let action = ActionRequest {
        action_id: "flush-abort".into(),
        ..request()
    };
    let request = ServiceRequest::Prepare {
        envelope: RequestEnvelope {
            protocol_version: PROTOCOL_VERSION,
            auth: AuthContext {
                actor_id: "actor".into(),
                account_id: "account".into(),
                token: "token".into(),
            },
            claimed_digest: request_digest(&action),
            request: action.clone(),
        },
        content: Some(b"flushed but unpublished".to_vec()),
        fingerprint: "flush".into(),
    };
    stream
        .1
        .write_all(serde_json::to_string(&request).unwrap().as_bytes())
        .await
        .unwrap();
    stream.1.write_all(b"\n").await.unwrap();
    assert!(!child.wait().unwrap().success());
    drop(stream);
    let mut reopened_child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            catalog.to_str().unwrap(),
            lore.to_str().unwrap(),
            "account",
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .spawn()
        .unwrap();
    let mut reopened = loop {
        match UnixStream::connect(&socket).await {
            Ok(stream) => break stream.into_split(),
            Err(_) => {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        }
    };
    let result = send(
        &mut reopened,
        ServiceRequest::Reconcile {
            envelope: control(&action.action_id),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::Unknown,
                fingerprint: None,
            },
        },
    )
    .await;
    assert!(matches!(
        result,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::NeedsReconciliation,
            before: None,
            ..
        })
    ));
    let resolved = send(
        &mut reopened,
        ServiceRequest::Reconcile {
            envelope: control(&action.action_id),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::NotCommitted,
                fingerprint: None,
            },
        },
    )
    .await;
    assert!(matches!(
        resolved,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    assert_eq!(
        send(
            &mut reopened,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Accepted
    );
    assert!(reopened_child.wait().unwrap().success());
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn child_abort_before_lore_write_reopens_without_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("before-lore.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            catalog.to_str().unwrap(),
            lore.to_str().unwrap(),
            "account",
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .env("OPENCLANK_HISTORY_ABORT_STAGE", "before_lore")
        .spawn()
        .unwrap();
    let mut stream = connect(&socket).await;
    let action = ActionRequest {
        action_id: "before-lore-abort".into(),
        ..request()
    };
    let request = ServiceRequest::Prepare {
        envelope: RequestEnvelope {
            protocol_version: PROTOCOL_VERSION,
            auth: AuthContext {
                actor_id: "actor".into(),
                account_id: "account".into(),
                token: "token".into(),
            },
            claimed_digest: request_digest(&action),
            request: action.clone(),
        },
        content: Some(b"never written".to_vec()),
        fingerprint: "before".into(),
    };
    stream
        .1
        .write_all(serde_json::to_string(&request).unwrap().as_bytes())
        .await
        .unwrap();
    stream.1.write_all(b"\n").await.unwrap();
    assert!(!child.wait().unwrap().success());
    drop(stream);
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            catalog.to_str().unwrap(),
            lore.to_str().unwrap(),
            "account",
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .spawn()
        .unwrap();
    let mut stream = connect(&socket).await;
    let resolved = send(
        &mut stream,
        ServiceRequest::Reconcile {
            envelope: control(&action.action_id),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::Unknown,
                fingerprint: None,
            },
        },
    )
    .await;
    assert!(matches!(
        resolved,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::NeedsReconciliation,
            before: None,
            ..
        })
    ));
    let aborted = send(
        &mut stream,
        ServiceRequest::Reconcile {
            envelope: control(&action.action_id),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::NotCommitted,
                fingerprint: None,
            },
        },
    )
    .await;
    assert!(matches!(
        aborted,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            before: None,
            ..
        })
    ));
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Accepted
    );
    assert!(child.wait().unwrap().success());
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn child_abort_applying_and_after_durable_reopen_without_live_replay() {
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("applying.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let spawn = |stage: Option<&str>| {
        let mut command = std::process::Command::new(bin);
        command
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "account",
            ])
            .env("OPENCLANK_HISTORY_TOKEN", "token");
        if let Some(stage) = stage {
            command.env("OPENCLANK_HISTORY_ABORT_STAGE", stage);
        }
        command.spawn().unwrap()
    };
    let mut child = spawn(None);
    let mut stream = connect(&socket).await;
    let action = ActionRequest {
        action_id: "applying-abort".into(),
        ..request()
    };
    let env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&action),
        request: action.clone(),
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: env,
                content: Some(b"before applying".to_vec()),
                fingerprint: "before".into()
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    child.kill().unwrap();
    child.wait().unwrap();
    drop(stream);
    let mut child = spawn(Some("applying"));
    let mut stream = connect(&socket).await;
    let live_request = ServiceRequest::RecordLive {
        envelope: control(&action.action_id),
        receipt: LiveReceipt { committed_resources: Vec::new(),
            action_id: action.action_id.clone(),
            status: LiveStatus::Committed,
            fingerprint: Some("must-not-replay".into()),
        },
    };
    stream
        .1
        .write_all(serde_json::to_string(&live_request).unwrap().as_bytes())
        .await
        .unwrap();
    stream.1.write_all(b"\n").await.unwrap();
    assert!(!child.wait().unwrap().success());
    drop(stream);
    let mut child = spawn(None);
    let mut stream = connect(&socket).await;
    let reconciled = send(
        &mut stream,
        ServiceRequest::Reconcile {
            envelope: control(&action.action_id),
            receipt: LiveReceipt { committed_resources: Vec::new(),
                action_id: action.action_id.clone(),
                status: LiveStatus::NotCommitted,
                fingerprint: None,
            },
        },
    )
    .await;
    assert!(matches!(
        reconciled,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Accepted
    );
    assert!(child.wait().unwrap().success());
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn child_abort_committed_before_after_capture_marks_uncertain_without_replay() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("committed-before-after.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let spawn = |stage: Option<&str>| {
        let mut command = std::process::Command::new(bin);
        command
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "account",
            ])
            .env("OPENCLANK_HISTORY_TOKEN", "token");
        if let Some(stage) = stage {
            command.env("OPENCLANK_HISTORY_ABORT_STAGE", stage);
        }
        command.spawn().unwrap()
    };
    let mut child = spawn(None);
    let mut stream = connect(&socket).await;
    let action = ActionRequest {
        action_id: "committed-before-after".into(),
        ..request()
    };
    let env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&action),
        request: action.clone(),
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: env,
                content: Some(b"before".to_vec()),
                fingerprint: "before".into()
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::RecordLive {
                envelope: control(&action.action_id),
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::Committed,
                    fingerprint: Some("live".into())
                }
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    child.kill().unwrap();
    child.wait().unwrap();
    drop(stream);
    let mut child = spawn(Some("committed_before_after"));
    let mut stream = connect(&socket).await;
    let complete = ServiceRequest::Complete {
        envelope: control(&action.action_id),
        content: Some(b"after".to_vec()),
        fingerprint: "after".into(),
    };
    stream
        .1
        .write_all(serde_json::to_string(&complete).unwrap().as_bytes())
        .await
        .unwrap();
    stream.1.write_all(b"\n").await.unwrap();
    assert!(!child.wait().unwrap().success());
    drop(stream);
    let mut child = spawn(None);
    let mut stream = connect(&socket).await;
    let retry_env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&action),
        request: action.clone(),
    };
    let record = match send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: retry_env,
            content: Some(b"before".to_vec()),
            fingerprint: "before".into(),
        },
    )
    .await
    {
        ServiceResponse::Action(record) => record,
        other => panic!("unexpected retry: {other:?}"),
    };
    assert_eq!(record.state, ActionState::CapturingAfter);
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Reconcile {
                envelope: control(&action.action_id),
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::Unknown,
                    fingerprint: None
                }
            }
        )
        .await,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::NeedsReconciliation,
            ..
        })
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Reconcile {
                envelope: control(&action.action_id),
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::NotCommitted,
                    fingerprint: None
                }
            }
        )
        .await,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Accepted
    );
    assert!(child.wait().unwrap().success());
}

#[test]
fn startup_failure_after_lock_creation_cleans_installation_artifacts() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("startup.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    for stage in ["pid", "bind", "chmod"] {
        let status = std::process::Command::new(bin)
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "account",
            ])
            .env("OPENCLANK_HISTORY_TOKEN", "token")
            .env("OPENCLANK_HISTORY_FAIL_STARTUP", stage)
            .status()
            .unwrap();
        assert!(
            !status.success(),
            "startup stage {stage} unexpectedly succeeded"
        );
        assert!(!socket.exists(), "socket leaked at {stage}");
        assert!(
            !dir.path().join("startup.sock.lock").exists(),
            "lock leaked at {stage}"
        );
    }
}

#[tokio::test]
async fn stalled_large_response_does_not_hold_coordinator_guard() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("backpressure.sock");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
            "account",
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .spawn()
        .unwrap();
    let mut stalled = connect(&socket).await;
    let mut action = ActionRequest {
        action_id: "large-response".into(),
        ..request()
    };
    action.coverage = Some(openclank_history::catalog::CaptureManifest {
        metadata: Some(serde_json::json!({"large": "x".repeat(700_000)})),
        ..Default::default()
    });
    let request = ServiceRequest::Prepare {
        envelope: RequestEnvelope {
            protocol_version: PROTOCOL_VERSION,
            auth: AuthContext {
                actor_id: "actor".into(),
                account_id: "account".into(),
                token: "token".into(),
            },
            claimed_digest: request_digest(&action),
            request: action,
        },
        content: Some(b"small".to_vec()),
        fingerprint: "small".into(),
    };
    stalled
        .1
        .write_all(serde_json::to_string(&request).unwrap().as_bytes())
        .await
        .unwrap();
    stalled.1.write_all(b"\n").await.unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    let mut health = connect(&socket).await;
    let response = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        send(
            &mut health,
            ServiceRequest::Health(control("large-response")),
        ),
    )
    .await
    .unwrap();
    assert!(matches!(response, ServiceResponse::Health { .. }));
    assert_eq!(
        send(
            &mut health,
            ServiceRequest::Shutdown(control("large-response"))
        )
        .await,
        ServiceResponse::Error {
            code: "active actions prevent checked shutdown".into()
        }
    );
    drop(stalled);
    child.kill().unwrap();
    child.wait().unwrap();
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn child_abort_after_durable_capture_preserves_after_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("after-durable.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let spawn = |stage: Option<&str>| {
        let mut command = std::process::Command::new(bin);
        command
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "account",
            ])
            .env("OPENCLANK_HISTORY_TOKEN", "token");
        if let Some(stage) = stage {
            command.env("OPENCLANK_HISTORY_ABORT_STAGE", stage);
        }
        command.spawn().unwrap()
    };
    let mut child = spawn(None);
    let mut stream = connect(&socket).await;
    let action = ActionRequest {
        action_id: "after-durable-abort".into(),
        ..request()
    };
    let env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&action),
        request: action.clone(),
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: env,
                content: Some(b"before".to_vec()),
                fingerprint: "before".into()
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::RecordLive {
                envelope: control(&action.action_id),
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::Committed,
                    fingerprint: Some("live".into())
                }
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    child.kill().unwrap();
    child.wait().unwrap();
    drop(stream);
    let mut child = spawn(Some("after_durable"));
    let mut stream = connect(&socket).await;
    let complete = ServiceRequest::Complete {
        envelope: control(&action.action_id),
        content: Some(b"after".to_vec()),
        fingerprint: "after".into(),
    };
    stream
        .1
        .write_all(serde_json::to_string(&complete).unwrap().as_bytes())
        .await
        .unwrap();
    stream.1.write_all(b"\n").await.unwrap();
    assert!(!child.wait().unwrap().success());
    drop(stream);
    let mut child = spawn(None);
    let mut stream = connect(&socket).await;
    let retry_env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&action),
        request: action.clone(),
    };
    let record = match send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: retry_env,
            content: Some(b"before".to_vec()),
            fingerprint: "before".into(),
        },
    )
    .await
    {
        ServiceResponse::Action(record) => record,
        other => panic!("unexpected retry: {other:?}"),
    };
    assert_eq!(record.state, ActionState::AfterDurable);
    let after = record.after.clone().unwrap();
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::ReadVersion {
                envelope: control(&action.action_id),
                receipt: after
            }
        )
        .await,
        ServiceResponse::Bytes(Some(b"after".to_vec()))
    );
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Reconcile {
                envelope: control(&action.action_id),
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::NotCommitted,
                    fingerprint: None
                }
            }
        )
        .await,
        ServiceResponse::Action(ActionRecord {
            state: ActionState::Aborted,
            ..
        })
    ));
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Accepted
    );
    assert!(child.wait().unwrap().success());
}

#[tokio::test]
#[cfg(feature = "test_faults")]
async fn child_abort_after_complete_keeps_idempotent_complete_receipt() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("complete-abort.sock");
    let catalog = dir.path().join("catalog.redb");
    let lore = dir.path().join("lore");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let spawn = |stage: Option<&str>| {
        let mut command = std::process::Command::new(bin);
        command
            .args([
                socket.to_str().unwrap(),
                catalog.to_str().unwrap(),
                lore.to_str().unwrap(),
                "account",
            ])
            .env("OPENCLANK_HISTORY_TOKEN", "token");
        if let Some(stage) = stage {
            command.env("OPENCLANK_HISTORY_ABORT_STAGE", stage);
        }
        command.spawn().unwrap()
    };
    let mut child = spawn(None);
    let mut stream = connect(&socket).await;
    let action = ActionRequest {
        action_id: "complete-abort".into(),
        ..request()
    };
    let env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&action),
        request: action.clone(),
    };
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::Prepare {
                envelope: env,
                content: Some(b"before".to_vec()),
                fingerprint: "before".into()
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    assert!(matches!(
        send(
            &mut stream,
            ServiceRequest::RecordLive {
                envelope: control(&action.action_id),
                receipt: LiveReceipt { committed_resources: Vec::new(),
                    action_id: action.action_id.clone(),
                    status: LiveStatus::Committed,
                    fingerprint: Some("live".into())
                }
            }
        )
        .await,
        ServiceResponse::Action(_)
    ));
    child.kill().unwrap();
    child.wait().unwrap();
    drop(stream);
    let mut child = spawn(Some("complete"));
    let mut stream = connect(&socket).await;
    let complete = ServiceRequest::Complete {
        envelope: control(&action.action_id),
        content: Some(b"after".to_vec()),
        fingerprint: "after".into(),
    };
    stream
        .1
        .write_all(serde_json::to_string(&complete).unwrap().as_bytes())
        .await
        .unwrap();
    stream.1.write_all(b"\n").await.unwrap();
    assert!(!child.wait().unwrap().success());
    drop(stream);
    let mut child = spawn(None);
    let mut stream = connect(&socket).await;
    let retry_env = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&action),
        request: action.clone(),
    };
    let record = match send(
        &mut stream,
        ServiceRequest::Prepare {
            envelope: retry_env,
            content: Some(b"before".to_vec()),
            fingerprint: "before".into(),
        },
    )
    .await
    {
        ServiceResponse::Action(record) => record,
        other => panic!("unexpected complete retry: {other:?}"),
    };
    assert_eq!(record.state, ActionState::Complete);
    let after = record.after.unwrap();
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::ReadVersion {
                envelope: control(&action.action_id),
                receipt: after
            }
        )
        .await,
        ServiceResponse::Bytes(Some(b"after".to_vec()))
    );
    assert_eq!(
        send(
            &mut stream,
            ServiceRequest::Shutdown(control(&action.action_id))
        )
        .await,
        ServiceResponse::Accepted
    );
    assert!(child.wait().unwrap().success());
}

#[tokio::test]
async fn idle_client_does_not_block_health_admission() {
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("idle.sock");
    let bin = env!("CARGO_BIN_EXE_openclank-history-service");
    let mut child = std::process::Command::new(bin)
        .args([
            socket.to_str().unwrap(),
            dir.path().join("catalog.redb").to_str().unwrap(),
            dir.path().join("lore").to_str().unwrap(),
            "account",
        ])
        .env("OPENCLANK_HISTORY_TOKEN", "token")
        .spawn()
        .unwrap();
    let idle = loop {
        match UnixStream::connect(&socket).await {
            Ok(stream) => break stream,
            Err(_) => {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        }
    };
    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
    let mut health = UnixStream::connect(&socket).await.unwrap().into_split();
    let response = send(&mut health, ServiceRequest::Health(control("idle"))).await;
    assert!(matches!(response, ServiceResponse::Health { .. }));
    drop(idle);
    assert_eq!(
        send(&mut health, ServiceRequest::Shutdown(control("idle"))).await,
        ServiceResponse::Accepted
    );
    assert!(child.wait().unwrap().success());
}
