"""Auth-disabled compatibility mapping and owner-identity predicates.

Pins Astra's mapping shape (host/runtime audit 08) and O01's "no silent
identity migration" rule:

- unnamed auth-disabled scope maps to provider ``local-installation`` and
  Copal ``local`` — existing domain adapters, not synthetic human users
- a delegated API token's attributed owner is never administrator authority
- Default/Local/__odysseus_local__ are refused as stored human identities
- explicit auth-disabled and first-run loopback remain distinguishable
"""

import pytest

from src import owner_identity as oid
from src.auth_helpers import copal_owner_for_user


def test_unnamed_scope_maps_to_local_installation_provider():
    assert oid.provider_owner_for("") == "local-installation"
    assert oid.provider_owner_for(None) == "local-installation"
    assert oid.provider_owner_for("   ") == "local-installation"
    assert oid.provider_owner_for("alice") == "alice"
    assert oid.provider_owner_for("Alice") == "alice"


def test_unnamed_scope_maps_to_copal_local():
    assert oid.copal_owner_for("") == "local"
    assert oid.copal_owner_for(None) == "local"
    # Route helper agrees with the predicate.
    assert copal_owner_for_user("") == "local"
    assert copal_owner_for_user(None) == "local"
    assert copal_owner_for_user("alice") == "alice"


def test_copal_reserved_names_are_namespaced_not_collapsed():
    # A real human account named like a Copal sentinel must not impersonate it.
    assert copal_owner_for_user("local") == "user:local"
    assert copal_owner_for_user("shared") == "user:shared"
    assert oid.copal_owner_for("local") == "local"  # predicate is raw mapping


def test_provider_and_copal_scopes_stay_distinct():
    # The two domain adapters must not collapse into one store identity.
    assert oid.provider_owner_for("") != oid.copal_owner_for("")


def test_scoped_token_never_implies_admin():
    assert oid.token_implies_admin("alice") is False
    assert oid.token_implies_admin(None) is False
    assert oid.token_implies_admin("admin") is False


def test_default_and_local_identities_are_refused_as_stored_users():
    for name in ("Default", "default", "Local", "local", "__odysseus_local__",
                 "internal-tool", "api", "local-installation"):
        assert oid.is_reserved_stored_identity(name), name
    assert oid.is_reserved_stored_identity("user:alice")
    assert oid.is_reserved_stored_identity("deleted:alice")
    assert oid.is_reserved_stored_identity("agent:alice")
    assert not oid.is_reserved_stored_identity("alice")
    assert not oid.is_reserved_stored_identity("")


def test_actor_binding_preserves_named_owner_and_prefixes_agent_lane():
    assert oid.actor_for("alice") == "alice"
    assert oid.actor_for("alice", lane="agent") == "agent:alice"
    assert oid.actor_for("") == "local-installation"
    assert oid.actor_for("", lane="agent") == "agent:local-installation"


def test_account_id_prefers_stable_account_record():
    assert oid.account_id_for({"account_id": "acct-9"}, "alice") == "acct-9"
    assert oid.account_id_for({}, "alice") == "alice"
    assert oid.account_id_for(None, None) == "local-installation"


def test_auth_disabled_and_first_run_loopback_are_different_cases(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    assert oid.auth_disabled() is False
    assert oid.localhost_bypass_enabled() is False

    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert oid.auth_disabled() is True
    assert oid.localhost_bypass_enabled() is False

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LOCALHOST_BYPASS", "true")
    assert oid.auth_disabled() is False
    assert oid.localhost_bypass_enabled() is True


def test_internal_tool_and_api_pseudo_identities():
    assert oid.is_internal_tool_identity("internal-tool")
    assert not oid.is_internal_tool_identity("alice")
    assert oid.is_api_pseudo_user("api")
    assert not oid.is_api_pseudo_user("alice")


def test_no_synthetic_default_local_users_created_by_mapping():
    # Mapping must never produce Default/Local as a store key.
    for scope in ("", None, "   "):
        provider = oid.provider_owner_for(scope)
        copal = oid.copal_owner_for(scope)
        assert provider == "local-installation"
        assert copal == "local"
        assert not oid.is_reserved_stored_identity(provider) or provider == "local-installation"
        # Copal "local" is a Copal sentinel, not a human account.
        assert copal in ("local",)
