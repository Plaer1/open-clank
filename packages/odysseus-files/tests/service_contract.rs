use odysseus_files::*;
use serde_json::json;
use std::fs;
use std::path::Path;
use tempfile::tempdir;

fn registry_for(root: &Path) -> RootRegistry {
    let canonical = fs::canonicalize(root).unwrap();
    let mut registry = RootRegistry::default();
    registry
        .register(RootRecord {
            id: "root-1".into(),
            owner_id: "owner-1".into(),
            kind: RootKind::RecursiveDirectory,
            canonical_path: canonical.to_string_lossy().into_owned(),
            display_path: canonical.to_string_lossy().into_owned(),
            enabled: true,
            capabilities: [Capability::Read, Capability::Write].into_iter().collect(),
            platform_identity: PlatformIdentity {
                volume_id: None,
                file_id: None,
                device: None,
                inode: None,
                case_sensitive: Some(true),
            },
            last_validated_unix_ms: None,
            availability: RootAvailability::Available,
        })
        .unwrap();
    registry
}

fn request(operation: Operation, target: Target, payload: serde_json::Value) -> ClientRequest {
    ClientRequest::new(
        operation,
        target,
        "request-1",
        "client-session",
        "root-op",
        payload,
    )
}

#[test]
fn app_service_dispatches_browse_and_read_without_agent_scope() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("outside.md");
    fs::write(&file, b"hello app").unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::app("owner-1", "ui", "service-session"),
    );
    let read = service
        .dispatch(request(
            Operation::ReadRange,
            Target::path(file.to_string_lossy()),
            json!({"length": 32}),
        ))
        .unwrap();
    assert_eq!(
        read["data"]["bytes"],
        json!([104, 101, 108, 108, 111, 32, 97, 112, 112])
    );
    assert_eq!(read["audit_id"], "audit:request-1");
    assert_eq!(
        service
            .dispatch(request(Operation::Health, Target::path("/"), json!({})))
            .unwrap()["lane"],
        "app"
    );
}

#[test]
fn service_dispatches_directory_limits_for_every_lane_and_sort_path() {
    let directory = tempdir().unwrap();
    for index in 0..201 {
        fs::write(
            directory.path().join(format!("{index:03}.txt")),
            index.to_string(),
        )
        .unwrap();
    }
    let config = EngineConfig {
        directory_page_size: 200,
        ..EngineConfig::default()
    };
    let app = FileService::new(
        FileEngine::new(config.clone(), registry_for(directory.path())),
        ServiceIdentity::app("owner-1", "ui", "service-session"),
    );
    let agent = FileService::new(
        FileEngine::new(config, registry_for(directory.path())),
        ServiceIdentity::agent(
            "owner-1",
            "agent-1",
            "service-session",
            AgentScope::new(["root-1".into()]),
            [Capability::Read],
        ),
    );
    let target = || Target::path(directory.path().to_string_lossy());
    let sorted = json!({
        "limit": 37,
        "sort": {
            "key": "name",
            "direction": "asc",
            "directories_first": true,
            "collation": "open-clank-v1"
        }
    });

    for response in [
        app.dispatch(request(
            Operation::ListDirectory,
            target(),
            json!({"limit": 37}),
        ))
        .unwrap(),
        app.dispatch(request(Operation::ListDirectory, target(), sorted.clone()))
            .unwrap(),
        agent
            .dispatch(request(
                Operation::ListDirectory,
                target(),
                json!({"limit": 37}),
            ))
            .unwrap(),
        agent
            .dispatch(request(Operation::ListDirectory, target(), sorted))
            .unwrap(),
    ] {
        assert_eq!(response["data"]["entries"].as_array().unwrap().len(), 37);
        assert!(response["data"]["next_cursor"].is_object());
    }
}

#[test]
fn service_rejects_directory_limits_outside_advertised_bounds() {
    let directory = tempdir().unwrap();
    let service = FileService::new(
        FileEngine::new(
            EngineConfig {
                directory_page_size: 200,
                ..EngineConfig::default()
            },
            registry_for(directory.path()),
        ),
        ServiceIdentity::app("owner-1", "ui", "service-session"),
    );
    let target = || Target::path(directory.path().to_string_lossy());

    for limit in [0, 201] {
        let error = service
            .dispatch(request(
                Operation::ListDirectory,
                target(),
                json!({"limit": limit}),
            ))
            .unwrap_err();
        assert_eq!(error.code, ProtocolErrorCode::MalformedRequest);
        assert_eq!(
            error.message,
            "list_directory limit must be between 1 and 200"
        );
    }
}

#[test]
fn service_exposes_the_bounded_text_preview_operation() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("large.txt");
    let body = format!("HEAD\n{}\nTAIL", "x".repeat(2 * 1024 * 1024));
    fs::write(&file, body).unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::app("owner-1", "ui", "service-session"),
    );

    let preview = service
        .dispatch(request(
            Operation::ReadTextPreview,
            Target::path(file.to_string_lossy()),
            json!({"max_bytes": 320000}),
        ))
        .unwrap();

    assert_eq!(preview["data"]["truncated"], true);
    assert_eq!(preview["data"]["work"]["bytes_read"], 320_000);
    assert!(preview["data"]["text"]
        .as_str()
        .unwrap()
        .starts_with("HEAD\n"));
    assert!(preview["data"]["text"]
        .as_str()
        .unwrap()
        .ends_with("\nTAIL"));
}

#[test]
fn stat_is_scoped_and_fingerprint_is_opt_in() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("stat.txt");
    fs::write(&file, b"stat me").unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::app_with_scope(
            "non-admin-1",
            "ui",
            "service-session",
            AppScope::assigned(["root-1".into()], [Capability::Read], 7),
        ),
    );
    let plain = service
        .dispatch(request(
            Operation::Stat,
            Target::path(file.to_string_lossy()),
            json!({}),
        ))
        .unwrap();
    assert_eq!(plain["data"]["kind"], json!("File"));
    assert!(plain["data"]["fingerprint"].is_null());
    let hashed = service
        .dispatch(request(
            Operation::Stat,
            Target::path(file.to_string_lossy()),
            json!({"include_fingerprint": true}),
        ))
        .unwrap();
    assert_eq!(hashed["data"]["fingerprint"]["algorithm"], json!("sha256"));
    let outside_dir = tempdir().unwrap();
    let outside = outside_dir.path().join("secret.txt");
    fs::write(&outside, b"secret").unwrap();
    let denied = service
        .dispatch(request(
            Operation::Stat,
            Target::path(outside.to_string_lossy()),
            json!({}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::Denied);
}

#[test]
fn stable_file_handle_reads_the_opened_object_and_rejects_forgery() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("handled.txt");
    fs::write(&file, b"descriptor bytes").unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::app_with_scope(
            "non-admin-1",
            "ui",
            "service-session",
            AppScope::assigned(["root-1".into()], [Capability::Read], 7),
        ),
    );

    let opened = service
        .dispatch(request(
            Operation::OpenHandle,
            Target::path(file.to_string_lossy()),
            json!({}),
        ))
        .unwrap();
    let handle: ResourceHandle = serde_json::from_value(opened["data"]["handle"].clone()).unwrap();
    assert_eq!(handle.token.len(), 64);
    assert_eq!(handle.generation, 7);
    assert!(handle.relative_components.is_empty());

    let read = service
        .dispatch(request(
            Operation::ReadRange,
            Target::handle(handle.clone()),
            json!({"offset": 0, "length": 128, "include_fingerprint": true}),
        ))
        .unwrap();
    assert_eq!(
        read["data"]["bytes"],
        json!([100, 101, 115, 99, 114, 105, 112, 116, 111, 114, 32, 98, 121, 116, 101, 115])
    );
    assert_eq!(read["data"]["fingerprint"]["algorithm"], "sha256");

    let stat = service
        .dispatch(request(
            Operation::Stat,
            Target::handle(handle.clone()),
            json!({}),
        ))
        .unwrap();
    assert_eq!(stat["data"]["size"], 16);

    let mut forged = handle.clone();
    forged.token.replace_range(..2, "ff");
    let denied = service
        .dispatch(request(
            Operation::ReadRange,
            Target::handle(forged),
            json!({"length": 1}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::StaleHandle);

    let mut changed_generation = handle.clone();
    changed_generation.generation += 1;
    let denied = service
        .dispatch(request(
            Operation::Stat,
            Target::handle(changed_generation),
            json!({}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::StaleHandle);

    let unsupported = service
        .dispatch(request(
            Operation::ReadLines,
            Target::handle(handle),
            json!({}),
        ))
        .unwrap_err();
    assert_eq!(unsupported.code, ProtocolErrorCode::Unsupported);
}

#[test]
fn stable_file_handle_fails_closed_on_replacement_and_cross_service_use() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("replace.txt");
    fs::write(&file, b"original").unwrap();
    let build = || {
        FileService::new(
            FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
            ServiceIdentity::app("owner-1", "ui", "service-session"),
        )
    };
    let service = build();
    let opened = service
        .dispatch(request(
            Operation::OpenHandle,
            Target::path(file.to_string_lossy()),
            json!({}),
        ))
        .unwrap();
    let handle: ResourceHandle = serde_json::from_value(opened["data"]["handle"].clone()).unwrap();

    let other_service = build();
    let cross_service = other_service
        .dispatch(request(
            Operation::ReadRange,
            Target::handle(handle.clone()),
            json!({"length": 8}),
        ))
        .unwrap_err();
    assert_eq!(cross_service.code, ProtocolErrorCode::StaleHandle);

    let displaced = directory.path().join("old.txt");
    fs::rename(&file, &displaced).unwrap();
    fs::write(&file, b"attacker replacement").unwrap();
    let replaced = service
        .dispatch(request(
            Operation::ReadRange,
            Target::handle(handle),
            json!({"length": 64}),
        ))
        .unwrap_err();
    assert_eq!(replaced.code, ProtocolErrorCode::StaleHandle);
}

#[test]
fn assigned_app_scope_authorizes_copy_and_move_as_two_path_operations() {
    let directory = tempdir().unwrap();
    let source = directory.path().join("source.txt");
    let copied = directory.path().join("copied.txt");
    let moved = directory.path().join("moved.txt");
    let renamed = directory.path().join("renamed.txt");
    fs::write(&source, b"copy me").unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::app_with_scope(
            "non-admin-1",
            "ui",
            "service-session",
            AppScope::assigned(["root-1".into()], [Capability::Read, Capability::Write], 7),
        ),
    );
    let copied_response = service
        .dispatch(request(
            Operation::Copy,
            Target::path(source.to_string_lossy()),
            json!({"destination": copied.to_string_lossy()}),
        ))
        .unwrap();
    assert_eq!(
        copied_response["data"]["fingerprint"]["algorithm"],
        json!("sha256")
    );
    assert_eq!(fs::read(&copied).unwrap(), b"copy me");

    service
        .dispatch(request(
            Operation::Move,
            Target::path(copied.to_string_lossy()),
            json!({"destination": moved.to_string_lossy()}),
        ))
        .unwrap();
    assert!(!copied.exists());
    assert_eq!(fs::read(&moved).unwrap(), b"copy me");

    service
        .dispatch(request(
            Operation::Rename,
            Target::path(moved.to_string_lossy()),
            json!({"destination": renamed.to_string_lossy()}),
        ))
        .unwrap();
    assert!(!moved.exists());
    assert_eq!(fs::read(&renamed).unwrap(), b"copy me");

    let created = directory.path().join("created.txt");
    service
        .dispatch(request(
            Operation::Create,
            Target::path(created.to_string_lossy()),
            json!({"text": "new file"}),
        ))
        .unwrap();
    assert_eq!(fs::read_to_string(&created).unwrap(), "new file");

    let new_directory = directory.path().join("new-folder");
    let made = service
        .dispatch(request(
            Operation::Mkdir,
            Target::path(new_directory.to_string_lossy()),
            json!({}),
        ))
        .unwrap();
    assert_eq!(
        made["data"],
        json!(fs::canonicalize(&new_directory)
            .unwrap()
            .to_string_lossy()
            .to_string())
    );
    assert!(new_directory.is_dir());

    let trash_response = service
        .dispatch(request(
            Operation::Trash,
            Target::path(created.to_string_lossy()),
            json!({}),
        ))
        .unwrap();
    assert!(!created.exists());
    let entry: TrashEntry = serde_json::from_value(trash_response["data"].clone()).unwrap();
    service
        .dispatch(request(
            Operation::Restore,
            Target::path(created.to_string_lossy()),
            json!({"entry": entry}),
        ))
        .unwrap();
    assert_eq!(fs::read_to_string(&created).unwrap(), "new file");

    let outside_dir = tempdir().unwrap();
    let outside = outside_dir.path().join("outside.txt");
    let denied = service
        .dispatch(request(
            Operation::Copy,
            Target::path(source.to_string_lossy()),
            json!({"destination": outside.to_string_lossy()}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::Denied);
    assert!(!outside.exists());
}

#[test]
fn assigned_app_scope_is_narrower_than_administrator_host_scope() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("assigned.md");
    fs::write(&file, b"assigned app").unwrap();
    let outside_dir = tempdir().unwrap();
    let outside = outside_dir.path().join("secret.md");
    fs::write(&outside, b"secret").unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::app_with_scope(
            "non-admin-1",
            "ui",
            "service-session",
            AppScope::assigned(["root-1".into()], [Capability::Read], 7),
        ),
    );
    let allowed = service
        .dispatch(request(
            Operation::ReadRange,
            Target::path(file.to_string_lossy()),
            json!({"length": 32}),
        ))
        .unwrap();
    assert_eq!(
        allowed["data"]["bytes"],
        json!([97, 115, 115, 105, 103, 110, 101, 100, 32, 97, 112, 112])
    );
    let denied = service
        .dispatch(request(
            Operation::ReadRange,
            Target::path(outside.to_string_lossy()),
            json!({"length": 32}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::Denied);
}

#[test]
fn assigned_app_scope_keeps_capabilities_bound_to_their_root() {
    let directory = tempdir().unwrap();
    let read_only = directory.path().join("read-only");
    let write_only = directory.path().join("write-only");
    fs::create_dir(&read_only).unwrap();
    fs::create_dir(&write_only).unwrap();
    let readable = read_only.join("readable.txt");
    fs::write(&readable, b"read me").unwrap();

    let mut registry = RootRegistry::default();
    for (id, root) in [("root-read", &read_only), ("root-write", &write_only)] {
        let canonical = fs::canonicalize(root).unwrap();
        registry
            .register(RootRecord {
                id: id.into(),
                owner_id: "administrator".into(),
                kind: RootKind::RecursiveDirectory,
                canonical_path: canonical.to_string_lossy().into_owned(),
                display_path: canonical.to_string_lossy().into_owned(),
                enabled: true,
                capabilities: [Capability::Read, Capability::Write].into_iter().collect(),
                platform_identity: PlatformIdentity {
                    volume_id: None,
                    file_id: None,
                    device: None,
                    inode: None,
                    case_sensitive: Some(true),
                },
                last_validated_unix_ms: None,
                availability: RootAvailability::Available,
            })
            .unwrap();
    }
    let scope = AppScope {
        host: false,
        visible_root_ids: ["root-read".into(), "root-write".into()]
            .into_iter()
            .collect(),
        capabilities: [Capability::Read, Capability::Write].into_iter().collect(),
        root_capabilities: [
            ("root-read".into(), [Capability::Read].into_iter().collect()),
            (
                "root-write".into(),
                [Capability::Write].into_iter().collect(),
            ),
        ]
        .into_iter()
        .collect(),
        generation: 7,
        active_folder: None,
    };
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry),
        ServiceIdentity::app_with_scope("non-admin-1", "ui", "service-session", scope),
    );

    let read = service
        .dispatch(request(
            Operation::ReadLines,
            Target::path(readable.to_string_lossy()),
            json!({}),
        ))
        .unwrap();
    assert_eq!(read["data"]["text"], "read me");

    let forbidden = read_only.join("must-not-exist.txt");
    let denied = service
        .dispatch(request(
            Operation::Create,
            Target::path(forbidden.to_string_lossy()),
            json!({"text": "forbidden"}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::Denied);
    assert!(!forbidden.exists());

    let allowed = write_only.join("allowed.txt");
    service
        .dispatch(request(
            Operation::Create,
            Target::path(allowed.to_string_lossy()),
            json!({"text": "allowed"}),
        ))
        .unwrap();
    assert_eq!(fs::read_to_string(&allowed).unwrap(), "allowed");

    let denied = service
        .dispatch(request(
            Operation::ReadLines,
            Target::path(allowed.to_string_lossy()),
            json!({}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::Denied);
}

#[test]
fn agent_service_requires_registered_scope_and_never_uses_payload_lane() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("inside.md");
    fs::write(&file, b"hello agent").unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::agent(
            "owner-1",
            "agent-1",
            "service-session",
            AgentScope::new(["root-1".into()]),
            [Capability::Read],
        ),
    );
    let response = service
        .dispatch(request(
            Operation::ReadRange,
            Target::path(file.to_string_lossy()),
            json!({"length": 32, "lane": "app"}),
        ))
        .unwrap();
    assert_eq!(
        response["data"]["bytes"],
        json!([104, 101, 108, 108, 111, 32, 97, 103, 101, 110, 116])
    );

    let outside_dir = tempdir().unwrap();
    let outside = outside_dir.path().join("outside.md");
    let denied = service
        .dispatch(request(
            Operation::ReadRange,
            Target::path(outside.to_string_lossy()),
            json!({"length": 32}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::Denied);
    assert_eq!(denied.message, "filesystem operation failed");
}

#[test]
fn unsupported_service_operation_is_typed() {
    let directory = tempdir().unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::app("owner-1", "ui", "service-session"),
    );
    let error = service
        .dispatch(request(
            Operation::WatchSubscribe,
            Target::path(directory.path().to_string_lossy()),
            json!({}),
        ))
        .unwrap_err();
    assert_eq!(error.code, ProtocolErrorCode::Unsupported);
}

#[test]
fn watch_authorization_is_directory_only_non_recursive_and_scoped() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("file.txt");
    fs::write(&file, "content").unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::agent(
            "owner-1",
            "agent-1",
            "service-session",
            AgentScope::new(["root-1".into()]),
            [Capability::Read],
        ),
    );

    let authorized = service
        .authorize_watch(request(
            Operation::WatchSubscribe,
            Target::path(directory.path().to_string_lossy()),
            json!({"recursive": false}),
        ))
        .unwrap();
    assert_eq!(authorized.path, fs::canonicalize(directory.path()).unwrap());
    assert_eq!(authorized.request_id, "request-1");

    let recursive = service
        .authorize_watch(request(
            Operation::WatchSubscribe,
            Target::path(directory.path().to_string_lossy()),
            json!({"recursive": true}),
        ))
        .unwrap_err();
    assert_eq!(recursive.code, ProtocolErrorCode::Denied);

    let regular_file = service
        .authorize_watch(request(
            Operation::WatchSubscribe,
            Target::path(file.to_string_lossy()),
            json!({}),
        ))
        .unwrap_err();
    assert_eq!(regular_file.code, ProtocolErrorCode::InvalidPath);

    let outside = tempdir().unwrap();
    let denied = service
        .authorize_watch(request(
            Operation::WatchSubscribe,
            Target::path(outside.path().to_string_lossy()),
            json!({}),
        ))
        .unwrap_err();
    assert_eq!(denied.code, ProtocolErrorCode::Denied);
}

#[test]
fn service_exposes_text_snapshot_and_atomic_patch_for_agent_lane() {
    let directory = tempdir().unwrap();
    let file = directory.path().join("edit.md");
    fs::write(&file, "before\n").unwrap();
    let service = FileService::new(
        FileEngine::new(EngineConfig::default(), registry_for(directory.path())),
        ServiceIdentity::agent(
            "owner-1",
            "agent-1",
            "service-session",
            AgentScope::new(["root-1".into()]),
            [Capability::Read, Capability::Write],
        ),
    );
    let snapshot = service
        .dispatch(request(
            Operation::ReadLines,
            Target::path(file.to_string_lossy()),
            json!({}),
        ))
        .unwrap();
    let fingerprint = snapshot["data"]["fingerprint"].clone();
    let patched = service
        .dispatch(request(
            Operation::Patch,
            Target::path(file.to_string_lossy()),
            json!({"expected_fingerprint": fingerprint, "old": "before", "new": "after"}),
        ))
        .unwrap();
    assert_eq!(patched["data"]["replacements"], 1);
    assert_eq!(fs::read_to_string(file).unwrap(), "after\n");
}
