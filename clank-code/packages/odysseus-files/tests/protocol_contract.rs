use odysseus_files::*;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::Path;

fn root(id: &str, owner: &str, kind: RootKind, path: &str) -> RootRecord {
    RootRecord {
        id: id.into(),
        owner_id: owner.into(),
        kind,
        canonical_path: path.into(),
        display_path: path.into(),
        enabled: true,
        capabilities: [Capability::Read, Capability::Write].into_iter().collect(),
        platform_identity: PlatformIdentity {
            volume_id: None,
            file_id: None,
            device: None,
            inode: None,
            case_sensitive: Some(true),
        },
        last_validated_unix_ms: Some(1),
        availability: RootAvailability::Available,
    }
}

#[test]
fn protocol_request_has_a_golden_shape_and_server_owned_lane() {
    let client = ClientRequest::new(
        Operation::ReadRange,
        Target::path("/workspace/src/main.rs"),
        "req-1",
        "session-1",
        "root-op-1",
        json!({"start": 0, "length": 10}),
    );
    let context = RequestContext::app(
        "owner-1",
        "ui",
        "req-1",
        "session-1",
        "root-op-1",
        Operation::ReadRange,
        Target::path("/workspace/src/main.rs"),
        "audit-1",
    )
    .with_scope(["root-1".to_string()], [Capability::Read], None)
    .with_deadline(Some(99));
    let envelope = context.bind(client).expect("valid request");
    let value = serde_json::to_value(&envelope).expect("serializes");
    assert_eq!(value["protocol"], json!({"major": 1, "minor": 0}));
    assert_eq!(value["context"]["lane"], "app");
    assert_eq!(value["context"]["approved_root_ids"], json!(["root-1"]));
    assert_eq!(value["operation"], "read_range");
    assert_eq!(envelope.context.lane(), TrustLane::App);
}

#[test]
fn list_directory_wire_payload_preserves_an_optional_per_request_limit() {
    let explicit = ClientRequest::new(
        Operation::ListDirectory,
        Target::path("/workspace"),
        "req-limit",
        "session-1",
        "root-op-1",
        json!({"limit": 37}),
    );
    let encoded = serde_json::to_value(&explicit).unwrap();
    assert_eq!(encoded["payload"], json!({"limit": 37}));
    let decoded: ClientRequest = serde_json::from_value(encoded).unwrap();
    assert_eq!(decoded.payload["limit"], 37);

    let compatible_default = ClientRequest::new(
        Operation::ListDirectory,
        Target::path("/workspace"),
        "req-default",
        "session-1",
        "root-op-1",
        json!({}),
    );
    assert!(compatible_default.payload.get("limit").is_none());
}

#[test]
fn client_cannot_inject_or_upgrade_the_trust_lane() {
    let forged = json!({
        "protocol": {"major": 1, "minor": 0},
        "request_id": "req-1",
        "session_id": "session-1",
        "root_operation_id": "root-op-1",
        "operation": "health",
        "target": {"kind": "path", "value": "/"},
        "lane": "agent",
        "payload": {}
    });
    assert!(serde_json::from_value::<ClientRequest>(forged).is_err());

    let request = ClientRequest::new(
        Operation::Health,
        Target::path("/"),
        "req-1",
        "session-1",
        "root-op-1",
        json!({}),
    );
    let context = RequestContext::app(
        "owner-1",
        "ui",
        "req-1",
        "session-1",
        "root-op-1",
        Operation::Health,
        Target::path("/"),
        "audit-1",
    );
    assert_eq!(
        context.bind(request).unwrap().context.lane(),
        TrustLane::App
    );
}

#[test]
fn app_scope_is_server_context_and_serializes_generation() {
    let context = RequestContext::app(
        "owner-1",
        "ui",
        "req-1",
        "session-1",
        "root-op-1",
        Operation::ReadRange,
        Target::path("/workspace/src/main.rs"),
        "audit-1",
    )
    .with_app_scope(AppScope::assigned(
        ["root-1".into()],
        [Capability::Read],
        42,
    ));
    let value = serde_json::to_value(&context).unwrap();
    assert_eq!(value["app_scope"]["host"], false);
    assert_eq!(value["app_scope"]["visible_root_ids"], json!(["root-1"]));
    assert_eq!(
        value["app_scope"]["root_capabilities"],
        json!({"root-1": ["read"]})
    );
    assert_eq!(value["app_scope"]["generation"], 42);
}

#[test]
fn protocol_mismatch_and_context_mismatch_are_typed_errors() {
    let mut request = ClientRequest::new(
        Operation::Health,
        Target::path("/"),
        "req-1",
        "session-1",
        "root-op-1",
        json!({}),
    );
    request.protocol = ProtocolVersion::new(2, 0);
    let context = RequestContext::app(
        "owner-1",
        "ui",
        "req-1",
        "session-1",
        "root-op-1",
        Operation::Health,
        Target::path("/"),
        "audit-1",
    );
    assert_eq!(
        context.bind(request).unwrap_err().code,
        ProtocolErrorCode::ProtocolMismatch
    );

    let request = ClientRequest::new(
        Operation::Stat,
        Target::path("/"),
        "req-1",
        "session-1",
        "root-op-1",
        json!({}),
    );
    let context = RequestContext::app(
        "owner-1",
        "ui",
        "req-1",
        "session-1",
        "root-op-1",
        Operation::Health,
        Target::path("/"),
        "audit-1",
    );
    assert_eq!(
        context.bind(request).unwrap_err().code,
        ProtocolErrorCode::MalformedRequest
    );
}

#[test]
fn registry_enforces_exact_files_recursive_dirs_and_component_boundaries() {
    let mut registry = RootRegistry::default();
    registry
        .register(root(
            "file",
            "owner-1",
            RootKind::ExactFile,
            "/approved/one.rs",
        ))
        .unwrap();
    registry
        .register(root(
            "dir",
            "owner-1",
            RootKind::RecursiveDirectory,
            "/approved",
        ))
        .unwrap();
    let read = [Capability::Read].into_iter().collect::<BTreeSet<_>>();
    let scope = AgentScope::new(["file".to_string(), "dir".to_string()]);

    assert!(registry
        .authorize("owner-1", Path::new("/approved/one.rs"), &read, &scope)
        .is_ok());
    assert_eq!(
        registry
            .authorize(
                "owner-1",
                Path::new("/approved/one.rs.bak"),
                &read,
                &AgentScope::new(["file".to_string()])
            )
            .unwrap_err(),
        RegistryError::OutsideRoot
    );
    assert!(registry
        .authorize("owner-1", Path::new("/approved/src/lib.rs"), &read, &scope)
        .is_ok());
    assert_eq!(
        registry
            .authorize(
                "owner-1",
                Path::new("/approved-sibling/a"),
                &read,
                &AgentScope::new(["dir".to_string()])
            )
            .unwrap_err(),
        RegistryError::OutsideRoot
    );
}

#[test]
fn registry_enforces_caps_disabled_owner_and_active_folder_narrowing() {
    let mut registry = RootRegistry::default();
    registry
        .register(root(
            "dir",
            "owner-1",
            RootKind::RecursiveDirectory,
            "/workspace",
        ))
        .unwrap();
    let mut write = BTreeSet::new();
    write.insert(Capability::Write);
    let scope = AgentScope::new(["dir".to_string()]);
    assert!(registry
        .authorize("owner-1", Path::new("/workspace/a"), &write, &scope)
        .is_ok());
    assert_eq!(
        registry
            .authorize("owner-2", Path::new("/workspace/a"), &write, &scope)
            .unwrap_err(),
        RegistryError::OutsideRoot
    );

    let folder = ActiveAgentFolder {
        id: "folder-1".into(),
        root_id: "dir".into(),
        canonical_path: "/workspace/src".into(),
        capabilities: [Capability::Read].into_iter().collect(),
    };
    let narrowed = scope.clone().with_active_folder(folder.clone());
    registry
        .validate_active_folder("owner-1", &folder, &narrowed)
        .unwrap();
    assert!(registry
        .authorize(
            "owner-1",
            Path::new("/workspace/src/lib.rs"),
            &BTreeSet::from([Capability::Read]),
            &narrowed
        )
        .is_ok());
    assert_eq!(
        registry
            .authorize(
                "owner-1",
                Path::new("/workspace/tests/a"),
                &BTreeSet::from([Capability::Read]),
                &narrowed
            )
            .unwrap_err(),
        RegistryError::OutsideRoot
    );
    assert_eq!(
        registry
            .authorize(
                "owner-1",
                Path::new("/workspace/src/lib.rs"),
                &write,
                &narrowed
            )
            .unwrap_err(),
        RegistryError::OutsideRoot
    );

    registry.disable("dir").unwrap();
    assert_eq!(
        registry
            .authorize(
                "owner-1",
                Path::new("/workspace/src/lib.rs"),
                &BTreeSet::from([Capability::Read]),
                &scope
            )
            .unwrap_err(),
        RegistryError::OutsideRoot
    );
}

#[test]
fn registry_allows_explicit_filesystem_root_and_rejects_empty_agent_scope() {
    let mut registry = RootRegistry::default();
    registry
        .register(root("root", "owner-1", RootKind::RecursiveDirectory, "/"))
        .unwrap();
    let scope = AgentScope::default();
    let read = BTreeSet::from([Capability::Read]);
    assert_eq!(
        registry
            .authorize("owner-1", Path::new("/tmp/a"), &read, &scope)
            .unwrap_err(),
        RegistryError::OutsideRoot
    );
}

#[test]
fn denied_errors_do_not_leak_paths_and_legacy_projection_is_explicit() {
    let error = ProtocolError::denied("audit-9");
    let serialized = serde_json::to_string(&error).unwrap();
    assert!(!serialized.contains("/secret"));
    assert!(!serialized.contains("workspace"));
    assert_eq!(error.audit_id.as_deref(), Some("audit-9"));

    let result = FileResult {
        protocol: PROTOCOL_VERSION,
        operation: Operation::ReadLines,
        path: "/workspace/README.md".into(),
        kind: "file".into(),
        range: None,
        page: None,
        bytes_considered: Some(42),
        lines_considered: Some(3),
        truncation: Some(Truncation {
            truncated: true,
            reason: Some("line_limit".into()),
            limit: Some(3),
        }),
        encoding: Some("utf-8".into()),
        newline: Some("lf".into()),
        media_type: Some("text/markdown".into()),
        fingerprint: Some("sha256:abc".into()),
        search_mode: None,
        items: vec![json!({"line": 1})],
        diagnostics: vec![],
        work: WorkCost::default(),
        audit_id: "audit-10".into(),
    };
    let legacy = result.to_legacy();
    assert_eq!(legacy.contract, LEGACY_RESULT_CONTRACT);
    assert_eq!(legacy.truncation_reason.as_deref(), Some("line_limit"));
    assert_eq!(legacy.fingerprint.as_deref(), Some("sha256:abc"));
    assert_eq!(legacy.items, vec![json!({"line": 1})]);
}

#[test]
fn transport_candidate_is_explicitly_unadmitted_until_measured() {
    let selection = TransportSelection::pending_local_ipc();
    assert_eq!(selection.primary, TransportKind::AuthenticatedLocalIpc);
    assert_eq!(selection.fallback, Some(TransportKind::FramedStdio));
    assert!(!selection.measured);
}

#[test]
fn stream_and_watcher_frames_preserve_gaps_and_cancellation_state() {
    let frame = StreamFrame {
        request_id: "req-2".into(),
        sequence: 7,
        done: false,
        state: CancellationState::Running,
        payload: Some(json!({"path": "/workspace/src/lib.rs"})),
        work: WorkCost {
            entries_visited: 1,
            ..WorkCost::default()
        },
        error: None,
    };
    let event = FileEvent {
        root_id: "root-1".into(),
        generation: 4,
        sequence: 8,
        kind: FileEventKind::Gap,
        path: None,
        old_path: None,
        fingerprint: None,
        observed_unix_ms: 100,
    };
    let round_trip: StreamFrame<Value> =
        serde_json::from_value(serde_json::to_value(frame).unwrap()).unwrap();
    assert_eq!(round_trip.state, CancellationState::Running);
    assert_eq!(round_trip.sequence, 7);
    assert_eq!(event.kind, FileEventKind::Gap);
    assert_eq!(event.path, None);
}

#[test]
fn path_resolution_makes_symlink_policy_and_escape_observable() {
    let resolution = PathResolution {
        input: "/workspace/link/file.rs".into(),
        canonical: Some("/outside/file.rs".into()),
        symlink_hops: 1,
        escaped_root: true,
        policy: SymlinkPolicy::AllowWithRevalidation,
    };
    let json = serde_json::to_value(&resolution).unwrap();
    assert_eq!(json["policy"], "allow_with_revalidation");
    assert_eq!(json["escaped_root"], true);
}

#[test]
fn portable_path_fixtures_cover_posix_prefixes_windows_drives_and_unc() {
    assert!(portable_component_contains(
        "/approved",
        "/approved/src/lib.rs",
        PathFlavor::Posix,
        true,
    ));
    assert!(!portable_component_contains(
        "/approved",
        "/approved-sibling/a",
        PathFlavor::Posix,
        true,
    ));
    assert!(!portable_component_contains(
        "/approved/src",
        "/approved/src/../../secret",
        PathFlavor::Posix,
        true,
    ));
    assert!(portable_component_contains(
        r"C:\Workspace",
        r"c:\workspace\src\main.rs",
        PathFlavor::Windows,
        false,
    ));
    assert!(!portable_component_contains(
        r"C:\Workspace",
        r"D:\Workspace\src\main.rs",
        PathFlavor::Windows,
        false,
    ));
    assert!(portable_component_contains(
        r"\\server\share\workspace",
        r"\\SERVER\SHARE\workspace\src",
        PathFlavor::Windows,
        false,
    ));
}

#[test]
fn framed_transport_handles_partial_reads_multiple_frames_and_backpressure() {
    let first = encode_frame(b"one", DEFAULT_MAX_FRAME_BYTES).unwrap();
    let second = encode_frame(b"two", DEFAULT_MAX_FRAME_BYTES).unwrap();
    let mut decoder = FrameDecoder::new(DEFAULT_MAX_FRAME_BYTES);
    assert!(decoder.feed(&first[..2]).unwrap().is_empty());
    assert!(decoder.feed(&first[2..]).unwrap() == vec![b"one".to_vec()]);
    let mut combined = first;
    combined.extend_from_slice(&second);
    assert_eq!(
        decoder.feed(&combined).unwrap(),
        vec![b"one".to_vec(), b"two".to_vec()]
    );
    assert_eq!(decoder.buffered_bytes(), 0);

    let oversized = encode_frame(b"12345", 4).unwrap_err();
    assert_eq!(oversized, FrameError::TooLarge);
    let mut bounded = FrameDecoder::new(4);
    let mut wire = vec![];
    wire.extend_from_slice(&(5_u32.to_be_bytes()));
    wire.extend_from_slice(b"12345");
    assert_eq!(bounded.feed(&wire).unwrap_err(), FrameError::TooLarge);
}
