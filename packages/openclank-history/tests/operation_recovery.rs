use openclank_history::catalog::{Catalog, ResourceKey};
use openclank_history::operations::{ActionRequest, request_digest};
use openclank_history::protocol::{
    AuthContext, PROTOCOL_VERSION, ProtocolError, RequestEnvelope, ServiceRequest, validate,
};

fn request() -> ActionRequest {
    ActionRequest {
        schema_version: 1,
        action_id: "retry".into(),
        actor_account_id: "account-id".into(),
        resource_key: ResourceKey {
            account_id: "account".into(),
            workspace_id: "workspace".into(),
            provider: "files".into(),
            resource_id: "resource".into(),
        },
        physical_lease_keys: vec![],
        guard_resource_ids: vec![ResourceKey {
            account_id: "account".into(),
            workspace_id: "workspace".into(),
            provider: "files".into(),
            resource_id: "guard".into(),
        }],
        modified_resource_ids: vec![ResourceKey {
            account_id: "account".into(),
            workspace_id: "workspace".into(),
            provider: "files".into(),
            resource_id: "resource".into(),
        }],
        operation: "replace".into(),
        expected_revision: Some("rev".into()),
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

fn envelope() -> RequestEnvelope {
    let request = request();
    RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account-id".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&request),
        request,
    }
}

#[test]
fn protocol_binds_version_token_actor_account_and_full_digest() {
    let valid = envelope();
    assert!(validate(&valid, "account-id", "token").is_ok());
    let mut wrong = valid.clone();
    wrong.auth.token = "wrong".into();
    assert_eq!(
        validate(&wrong, "account-id", "token"),
        Err(ProtocolError::InvalidToken)
    );
    let mut wrong = valid.clone();
    wrong.auth.account_id = "other".into();
    assert_eq!(
        validate(&wrong, "account-id", "token"),
        Err(ProtocolError::AccountMismatch)
    );
    let mut wrong = valid;
    wrong.claimed_digest = "stale".into();
    assert_eq!(
        validate(&wrong, "account-id", "token"),
        Err(ProtocolError::DigestMismatch)
    );
}

#[test]
fn client_cannot_forge_a_physical_lease_alias() {
    let mut forged = request();
    forged.physical_lease_keys = vec!["volume:inode:another-resource".into()];
    let envelope = RequestEnvelope {
        protocol_version: PROTOCOL_VERSION,
        auth: AuthContext {
            actor_id: "actor".into(),
            account_id: "account-id".into(),
            token: "token".into(),
        },
        claimed_digest: request_digest(&forged),
        request: forged,
    };
    assert_eq!(
        validate(&envelope, "account-id", "token"),
        Err(ProtocolError::UntrustedPhysicalLeaseIdentity)
    );

    let wire = serde_json::to_vec(&ServiceRequest::Prepare {
        envelope,
        content: None,
        fingerprint: "fp".into(),
    })
    .unwrap();
    let mut wire_value: serde_json::Value = serde_json::from_slice(&wire).unwrap();
    wire_value["Prepare"]["envelope"]["request"]["physical_lease_keys"] =
        serde_json::json!(["volume:inode:wire-forged"]);
    let decoded: ServiceRequest = serde_json::from_value(wire_value).unwrap();
    let ServiceRequest::Prepare { envelope, .. } = decoded else {
        panic!("expected prepare request")
    };
    assert!(envelope.request.physical_lease_keys.is_empty());
}

#[test]
fn owner_partitions_are_stable_across_rename_and_recreate() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("catalog.redb");
    let first = Catalog::open(&path, "account-1").unwrap();
    let original = first.owner_partition("account-1").unwrap();
    assert_ne!(original, [0; 16]);
    drop(first);
    let renamed = Catalog::open(&path, "account-1").unwrap();
    assert_eq!(renamed.owner_partition("account-1").unwrap(), original);
    drop(renamed);
    let other = Catalog::open(&path, "account-2").unwrap();
    assert_ne!(other.owner_partition("account-2").unwrap(), original);
    drop(other);
    let recreated = Catalog::open(&path, "account-3").unwrap();
    assert_ne!(recreated.owner_partition("account-3").unwrap(), original);
}
