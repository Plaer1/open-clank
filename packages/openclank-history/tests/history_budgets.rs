use openclank_history::retention::{
    BudgetError, DEFAULT_TOTAL_BYTES, ExpiryCandidate, HistoryPolicy, reserve_capture,
    select_expiry,
};
use openclank_history::usage::{MeasurementQuality, measure_root};
use openclank_history::catalog::Catalog;
use openclank_history::retention::{PersistedReservation, PolicySet, ScopeKind, ScopePolicy};
use std::fs;
use tempfile::tempdir;

#[test]
fn default_budget_reserves_before_capture_and_expires_oldest_eligible() {
    let policy = HistoryPolicy::default();
    assert_eq!(policy.total_bytes, DEFAULT_TOTAL_BYTES);
    let root = tempdir().unwrap();
    fs::write(root.path().join("payload"), b"1234").unwrap();
    let mut usage = measure_root(root.path(), 9).unwrap();
    assert!(matches!(
        usage.measurement_quality,
        MeasurementQuality::Allocated | MeasurementQuality::Apparent
    ));
    usage.reserved_inflight_bytes = DEFAULT_TOTAL_BYTES - usage.physical_allocated_bytes - 2;
    assert!(matches!(
        reserve_capture(&policy, &usage, 3, 1),
        Err(BudgetError::ExceedsTarget { .. })
    ));
    usage.reserved_inflight_bytes = 0;
    let reservation = reserve_capture(&policy, &usage, 3, 1).unwrap();
    assert_eq!(reservation.policy_revision, 1);
    let selected = select_expiry(
        vec![
            ExpiryCandidate {
                action_id: "new".into(),
                bytes: 10,
                created_millis: 2,
                pinned: false,
                active: false,
            },
            ExpiryCandidate {
                action_id: "old-pinned".into(),
                bytes: 10,
                created_millis: 1,
                pinned: true,
                active: false,
            },
            ExpiryCandidate {
                action_id: "old".into(),
                bytes: 10,
                created_millis: 0,
                pinned: false,
                active: false,
            },
        ],
        &openclank_history::usage::HistoryUsage {
            physical_allocated_bytes: DEFAULT_TOTAL_BYTES,
            ..usage
        },
        &policy,
        1,
    );
    assert_eq!(selected, vec!["old"]);
}

#[test]
fn policy_and_reservations_survive_reopen_and_are_atomic() {
    let root = tempdir().unwrap();
    let catalog_path = root.path().join("catalog.redb");
    let catalog = Catalog::open(&catalog_path, "account").unwrap();
    let mut policy = PolicySet::default();
    policy.scopes.push(ScopePolicy {
        scope_id: "workspace:one".into(),
        kind: ScopeKind::Workspace,
        owner_account_id: Some("account".into()),
        workspace_id: Some("workspace".into()),
        root: None,
        limit_bytes: Some(16),
        revision: 1,
        enabled: true,
    });
    let policy = catalog.set_policy(1, policy).unwrap();
    assert_eq!(policy.revision, 2);
    catalog
        .reserve_capture_scoped(
            PersistedReservation {
                reservation_id: "capture-1".into(),
                scope_id: "workspace:one".into(),
                bytes: 7,
                metadata_bytes: 1,
                policy_revision: policy.revision,
                created_millis: 1,
            },
            500,
            0,
        )
        .unwrap();
    assert_eq!(catalog.reserved_inflight_bytes().unwrap(), 8);
    drop(catalog);
    let reopened = Catalog::open(&catalog_path, "account").unwrap();
    assert_eq!(reopened.policy_set().unwrap().revision, 2);
    assert_eq!(reopened.reserved_inflight_bytes().unwrap(), 8);
    assert!(reopened
        .reserve_capture_scoped(
            PersistedReservation {
                reservation_id: "capture-2".into(),
                scope_id: "workspace:one".into(),
                bytes: 9,
                metadata_bytes: 0,
                policy_revision: 2,
                created_millis: 2,
            },
            500,
            9,
        )
        .is_err());
    assert!(reopened.release_capture_reservation("capture-1").unwrap());
}

#[test]
fn scoped_reservations_also_share_the_installation_physical_target() {
    let root = tempdir().unwrap();
    let catalog = Catalog::open(root.path().join("catalog.redb"), "account").unwrap();
    let mut policy = PolicySet::default();
    policy.global.total_bytes = 15;
    for scope_id in ["workspace:one", "workspace:two"] {
        policy.scopes.push(ScopePolicy {
            scope_id: scope_id.into(),
            kind: ScopeKind::Workspace,
            owner_account_id: Some("account".into()),
            workspace_id: Some(scope_id.replace("workspace:", "workspace").into()),
            root: None,
            limit_bytes: Some(100),
            revision: 1,
            enabled: true,
        });
    }
    let policy = catalog.set_policy(1, policy).unwrap();
    let reservation = |id: &str, scope: &str| PersistedReservation {
        reservation_id: id.into(),
        scope_id: scope.into(),
        bytes: 8,
        metadata_bytes: 0,
        policy_revision: policy.revision,
        created_millis: 1,
    };
    catalog.reserve_capture_scoped(reservation("one", "workspace:one"), 0, 0).unwrap();
    assert!(catalog.reserve_capture_scoped(reservation("two", "workspace:two"), 0, 0).is_err());
}
