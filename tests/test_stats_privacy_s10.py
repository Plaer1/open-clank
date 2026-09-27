import pytest

from services.stats.privacy import (
    IdentityCatalog,
    StatsIdentityError,
    identity_handle,
    owner_scope,
    safe_scope,
)


def test_owner_bound_handles_are_deterministic_and_not_cross_owner_authority():
    alice = identity_handle("alice", "session_id", "private-session")
    assert alice == identity_handle("alice", "session_id", "private-session")
    assert alice != identity_handle("bob", "session_id", "private-session")
    assert "private-session" not in alice
    assert owner_scope("alice") != owner_scope("bob")


def test_catalog_projects_safe_labels_and_rejects_foreign_or_unknown_handles():
    catalog = IdentityCatalog("alice", "actual_model", ["secret-z", "secret-a", "secret-z", None])
    assert catalog.project("secret-a")["label"] == "Model 1"
    assert catalog.project("secret-z")["label"] == "Model 2"
    assert all("secret" not in str(choice) for choice in catalog.choices())
    assert catalog.resolve(catalog.project("secret-z")["handle"]) == "secret-z"
    foreign = IdentityCatalog("bob", "actual_model", ["secret-z"]).project("secret-z")["handle"]
    with pytest.raises(StatsIdentityError):
        catalog.resolve(foreign)


def test_safe_scope_replaces_owner_and_identity_filters_without_mutating_source():
    raw = {
        "owner": "alice@example.test",
        "timezone": "Pacific/Honolulu",
        "filters": {"actual_model": "secret-model", "status": "complete"},
    }
    catalog = IdentityCatalog("alice@example.test", "actual_model", ["secret-model"])
    projected = safe_scope("alice@example.test", raw, catalogs={"actual_model": catalog})
    assert projected["owner_scope"] == owner_scope("alice@example.test")
    assert "owner" not in projected
    assert projected["filters"]["actual_model"]["label"] == "Model 1"
    assert "secret-model" not in str(projected)
    assert raw["owner"] == "alice@example.test"


def test_privacy_helpers_reject_blank_or_unsupported_authority():
    with pytest.raises(StatsIdentityError):
        owner_scope("")
    with pytest.raises(StatsIdentityError):
        identity_handle("alice", "hostname", "machine")
    with pytest.raises(StatsIdentityError):
        IdentityCatalog("alice", "actual_model", []).resolve("actual-model_missing")
