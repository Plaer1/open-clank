from __future__ import annotations

import pytest

import src.secret_storage as secret_storage
from src.openclank.resource_refs import (
    MAX_RESOURCE_REF_TTL_SECONDS,
    ResourceRefError,
    issue_resource_ref,
    resolve_resource_ref,
    resolve_resource_ref_for_reissue,
    stable_resource_id,
)


@pytest.fixture(autouse=True)
def isolated_app_key(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    yield
    secret_storage._fernet = None


def _issue(**overrides):
    values = {
        "owner_subject_id": "account-alice",
        "provider": "gallery",
        "origin_id": "image-42",
        "kind": "image",
        "capabilities": ("open", "preview", "download"),
        "policy_generation": 7,
        "now_unix_ms": 1_000_000,
    }
    values.update(overrides)
    return issue_resource_ref(**values)


def test_reference_is_opaque_owner_and_generation_bound():
    issued = _issue()
    assert "account-alice" not in issued.token
    assert "image-42" not in issued.token
    assert issued.public_dict()["id"] == issued.stable_id
    assert "origin_id" not in issued.public_dict()

    resolved = resolve_resource_ref(
        issued.token,
        expected_owner_subject_id="account-alice",
        current_policy_generation=7,
        required_capability="preview",
        now_unix_ms=1_000_001,
    )
    assert resolved.origin_id == "image-42"
    assert resolved.provider == "gallery"

    with pytest.raises(ResourceRefError) as owner_error:
        resolve_resource_ref(
            issued.token,
            expected_owner_subject_id="account-bob",
            current_policy_generation=7,
            now_unix_ms=1_000_001,
        )
    assert owner_error.value.code == "resource_unavailable"

    with pytest.raises(ResourceRefError) as generation_error:
        resolve_resource_ref(
            issued.token,
            expected_owner_subject_id="account-alice",
            current_policy_generation=8,
            now_unix_ms=1_000_001,
        )
    assert generation_error.value.code == "resource_ref_stale"


def test_stable_identity_ignores_display_names_but_separates_owner_and_provider():
    original = stable_resource_id(
        owner_subject_id="account-alice", provider="copal", origin_id="doc-1"
    )
    assert original == stable_resource_id(
        owner_subject_id="account-alice", provider="copal", origin_id="doc-1"
    )
    assert original != stable_resource_id(
        owner_subject_id="account-bob", provider="copal", origin_id="doc-1"
    )
    assert original != stable_resource_id(
        owner_subject_id="account-alice", provider="library", origin_id="doc-1"
    )


@pytest.mark.parametrize("kind", ["symlink", "special"])
def test_host_non_regular_kinds_are_truthful_and_opaque(kind):
    issued = _issue(provider="host", origin_id="host:v1:opaque", kind=kind, capabilities=("stat",))
    assert resolve_resource_ref(
        issued.token,
        expected_owner_subject_id="account-alice",
        current_policy_generation=7,
        required_capability="stat",
        now_unix_ms=1_000_001,
    ).kind == kind


def test_tamper_expiry_and_capability_checks_fail_closed():
    issued = _issue(ttl_seconds=60)
    tampered = issued.token[:-1] + ("A" if issued.token[-1] != "A" else "B")
    with pytest.raises(ResourceRefError):
        resolve_resource_ref(
            tampered,
            expected_owner_subject_id="account-alice",
            now_unix_ms=1_000_001,
        )
    with pytest.raises(ResourceRefError) as expired:
        resolve_resource_ref(
            issued.token,
            expected_owner_subject_id="account-alice",
            now_unix_ms=1_060_000,
        )
    assert expired.value.code == "resource_ref_stale"
    with pytest.raises(ResourceRefError) as capability:
        resolve_resource_ref(
            issued.token,
            expected_owner_subject_id="account-alice",
            required_capability="write",
            now_unix_ms=1_000_001,
        )
    assert capability.value.code == "resource_unavailable"


def test_reissue_resolver_recovers_only_authenticated_owner_identity():
    issued = _issue(ttl_seconds=60)
    recovered = resolve_resource_ref_for_reissue(
        issued.token,
        expected_owner_subject_id="account-alice",
        required_capability="open",
        now_unix_ms=1_060_000,
    )
    assert recovered.origin_id == "image-42"
    assert recovered.expires_unix_ms == 1_060_000

    with pytest.raises(ResourceRefError) as wrong_owner:
        resolve_resource_ref_for_reissue(
            issued.token,
            expected_owner_subject_id="account-bob",
            now_unix_ms=1_060_000,
        )
    assert wrong_owner.value.code == "resource_unavailable"

    with pytest.raises(ResourceRefError) as wrong_capability:
        resolve_resource_ref_for_reissue(
            issued.token,
            expected_owner_subject_id="account-alice",
            required_capability="write",
            now_unix_ms=1_060_000,
        )
    assert wrong_capability.value.code == "resource_unavailable"


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "invented"},
        {"kind": "invented"},
        {"capabilities": ("read", "invented")},
        {"policy_generation": -1},
        {"ttl_seconds": 0},
        {"ttl_seconds": MAX_RESOURCE_REF_TTL_SECONDS + 1},
    ],
)
def test_unknown_types_and_invalid_lifetimes_are_rejected(overrides):
    with pytest.raises(ResourceRefError):
        _issue(**overrides)
