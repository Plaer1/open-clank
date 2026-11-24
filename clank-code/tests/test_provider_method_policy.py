import pytest


def test_owner_policy_cas_and_reserved_preferences(tmp_path, monkeypatch):
    from routes import prefs_routes as prefs
    from src.openclank import provider_method_policy as policy

    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    prefs._save_for_user("alice", {"theme": "dark", "memory_policy": {"revision": 7}, "logging_preferences": {"level": "info"}}, allow_memory_policy=True, allow_logging=True)
    assert policy.get_policy("alice")["revision"] == 0
    assert not policy.get_policy("alice")["methods"][policy.METHOD]["enabled"]
    enabled = policy.set_policy("alice", enabled=True, expected_revision=0)
    assert policy.require_enabled("alice", enabled["revision"]) == 1
    assert policy.get_policy("bob")["revision"] == 0
    with pytest.raises(policy.ProviderMethodPolicyError, match="changed"):
        policy.set_policy("alice", enabled=False, expected_revision=0)
    with pytest.raises(policy.ProviderMethodPolicyError):
        policy.set_policy("alice", enabled=1, expected_revision=1)
    prefs._save_for_user("alice", {"theme": "light", "provider_method_policy": {"bad": True}})
    saved = prefs._load_for_user("alice")
    assert saved[policy.KEY] == enabled
    assert saved["memory_policy"] == {"revision": 7}
    assert saved["logging_preferences"] == {"level": "info"}
    assert policy.projection("alice")["managed_admission"]["scope"] == "actor_execution"
    assert policy.projection("alice")["auxiliary_admission"]["available"] is False
    policy.set_policy("alice", enabled=False, expected_revision=1)
    with pytest.raises(policy.ProviderMethodPolicyError) as exc:
        policy.require_enabled("alice", 1)
    assert exc.value.code == "beta_disabled"


def test_legacy_managed_claude_binding_is_fenced(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from routes import prefs_routes
    from src.openclank.provider_store import ProviderStore, ProviderValidationError
    from core.provider_models import ProviderConnection

    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "prefs.json"))
    connection = SimpleNamespace(family_id="anthropic", billing_lane="subscription")

    class Query:
        def __init__(self, model):
            self.model = model
        def filter_by(self, **kwargs):
            return self
        def first(self):
            return connection if self.model is ProviderConnection else None

    class Session:
        def query(self, model):
            return Query(model)
        def close(self):
            pass

    store = ProviderStore(session_factory=Session)
    with pytest.raises(ProviderValidationError, match="admission is unavailable"):
        store.handle_managed_method(owner="alice", holder_id="holder", method="_openclank/provider-store/v1/account/bind", params={"connectionID": "conn"})


def test_method_policy_proof_and_capability_contract():
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from src.openclank.managed_protocol import client_capability_offer, validate_initialize_result, ManagedProtocolError
    from src.openclank.provider_callbacks import ProviderMethodPolicyProof

    schema = json.loads(Path("contracts/openclank/managed-provider-v2.schema.json").read_text())
    validator = Draft202012Validator({"$ref": "#/$defs/providerMethodPolicy", "$defs": schema["$defs"]})
    proof = ProviderMethodPolicyProof("credential-owner", 3).to_wire()
    assert validator.is_valid(proof)
    assert not validator.is_valid({**proof, "policyRevision": 0})
    assert not validator.is_valid({**proof, "credentialOwner": "  "})
    assert not validator.is_valid({**proof, "methodID": "other"})
    offer = client_capability_offer()
    validate_initialize_result({"_meta": {"openclankManaged": offer}})
    old_offer = {k: v for k, v in offer.items() if k != "providerMethodPolicyContract"}
    with pytest.raises(ManagedProtocolError):
        validate_initialize_result({"_meta": {"openclankManaged": old_offer}})


def test_managed_binding_policy_is_frozen_and_rechecked(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.provider_models import ProviderBase, ProviderOperationBinding
    from src import secret_storage
    from routes import prefs_routes
    from src.openclank.provider_method_policy import set_policy
    from src.openclank.provider_store import ProviderStore, ProviderValidationError

    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / "key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine("sqlite://")
    ProviderBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    store = ProviderStore(factory)
    connection = store.create_connection(owner="alice", connection_id="conn", family_id="anthropic", adapter_id="anthropic", kind="official", billing_lane="subscription", label="test")
    account = store.create_account(owner="alice", connection_id=connection.id, account_id="acct", label="test", auth_method="oauth", auth_class="subscription", credentials={"type": "oauth", "access": "synthetic", "refresh": "synthetic", "expires": 9999999999999})
    route = store.create_model_route(owner="alice", connection_id=connection.id, model_route_id="route", provider_model_id="model", display_name="model", operations=("chat.stream",))
    store.set_entitlement(owner="alice", account_id=account.id, model_route_id=route.id, eligible=True, capability_fingerprint="test")
    params = {"rootOperationID":"root", "scopeKind":"actor_execution", "sessionID":"session", "actorID":"actor", "executionRevision":1, "modelRouteID":"route", "connectionID":"conn", "providerID":"anthropic", "billingLane":"subscription", "modelID":"model"}
    bind = lambda p: store.handle_managed_method(owner="alice", holder_id="holder", method="_openclank/provider-store/v1/account/bind", params=p)
    with pytest.raises(ProviderValidationError):
        bind(params)
    set_policy("alice", enabled=True, expected_revision=0)
    proof = {"version":1, "methodID":"anthropic.claude_subscription", "credentialOwner":"alice", "policyRevision":1}
    result = bind({**params, "methodPolicy":proof})
    assert result["methodPolicy"] == proof
    with factory() as db:
        snapshot = db.query(ProviderOperationBinding).filter_by(id=result["bindingID"]).one().policy_snapshot
        assert snapshot["openclank.provider_method_policy.v1"] == proof
        assert snapshot["ordered_account_ids"] == ["acct"]
    lease = store.handle_managed_method(owner="alice", holder_id="holder", method="_openclank/provider-store/v1/credential/lease", params={"bindingID":result["bindingID"], "expectedBindingRevision":result["bindingRevision"], "expectedCredentialRevision":result["credentialRevision"]})
    assert lease["methodPolicy"] == proof
    refresh = store.handle_managed_method(owner="alice", holder_id="holder", method="_openclank/provider-store/v1/refresh/acquire", params={"accountID":"acct", "connectionID":"conn", "expectedRevision":1, "bindingID":result["bindingID"], "methodPolicy":proof})
    set_policy("alice", enabled=False, expected_revision=1)
    with pytest.raises(ProviderValidationError):
        store.consume_credential_lease(owner="alice", holder_id="holder", lease_id=lease["leaseID"])
    with pytest.raises(ProviderValidationError):
        store.handle_managed_method(owner="alice", holder_id="holder", method="_openclank/provider-store/v1/account/commit", params={"bindingID":result["bindingID"], "expectedRevision":result["bindingRevision"]})
    set_policy("alice", enabled=True, expected_revision=2)
    with pytest.raises(ProviderValidationError):
        store.handle_managed_method(owner="alice", holder_id="holder", method="_openclank/provider-store/v1/refresh/commit", params={"leaseID":refresh["leaseID"], "accountID":"acct", "connectionID":"conn", "expectedRevision":1, "credential":{"type":"oauth", "access":"replacement", "refresh":"replacement", "expires":9999999999999}, "bindingID":result["bindingID"], "methodPolicy":proof})
    with pytest.raises(ProviderValidationError):
        bind({**params, "methodPolicy":proof})
    other = store.create_connection(owner="alice", connection_id="other", family_id="openai", adapter_id="openai", kind="official", billing_lane="metered_api", label="other")
    store.create_account(owner="alice", connection_id=other.id, account_id="other-acct", label="other", auth_method="api_key", auth_class="metered", credentials={"type":"api", "key":"synthetic"})
    other_route = store.create_model_route(owner="alice", connection_id=other.id, model_route_id="other-route", provider_model_id="other-model", display_name="other", operations=("chat.stream",))
    store.set_entitlement(owner="alice", account_id="other-acct", model_route_id=other_route.id, eligible=True, capability_fingerprint="test")
    other_binding = store.bind_account(owner="alice", root_operation_id="other-root", connection_id=other.id, model_route_id=other_route.id)
    other_lease = store.lease_credential(owner="alice", holder_id="holder", binding_id=other_binding.binding_id, expected_binding_revision=other_binding.binding_revision, expected_credential_revision=other_binding.credential_version)
    consumed = store.consume_credential_lease(owner="alice", holder_id="holder", lease_id=other_lease.lease_id)
    assert consumed.credentials["key"] == "synthetic"
    engine.dispose()


def test_shared_actor_refresh_success_and_reentry(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.provider_models import ProviderBase
    from src import secret_storage
    from routes import prefs_routes
    from src.openclank.provider_method_policy import set_policy
    from src.openclank.provider_store import ProviderStore, AccountSelector, ModelSelector

    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / "key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine("sqlite://")
    ProviderBase.metadata.create_all(engine)
    store = ProviderStore(sessionmaker(bind=engine, autoflush=False))
    store.create_connection(owner="alice", connection_id="shared", family_id="anthropic", adapter_id="anthropic", kind="official", billing_lane="subscription", label="test")
    store.create_account(owner="alice", connection_id="shared", account_id="account", label="test", auth_method="oauth", auth_class="subscription", credentials={"type":"oauth", "access":"old", "refresh":"old", "expires":9999999999999})
    store.create_model_route(owner="alice", connection_id="shared", model_route_id="route", provider_model_id="model", display_name="model", operations=("chat.stream",))
    store.set_entitlement(owner="alice", account_id="account", model_route_id="route", eligible=True, capability_fingerprint="test")
    grant = store.create_share_grant(owner="alice", recipient="bob", connection_id="shared", account_selector=AccountSelector.explicit(["account"]), model_selector=ModelSelector.explicit(["route"]), grant_id="grant")
    set_policy("alice", enabled=True, expected_revision=0)
    proof = {"version":1, "methodID":"anthropic.claude_subscription", "credentialOwner":"alice", "policyRevision":1}
    call = lambda method, params: store.handle_managed_method(owner="bob", holder_id="holder", method="_openclank/provider-store/v1/" + method, params=params)
    binding = call("account/bind", {"rootOperationID":"root", "scopeKind":"actor_execution", "sessionID":"session", "actorID":"actor", "executionRevision":1, "modelRouteID":"route", "connectionID":"shared", "providerID":"anthropic", "billingLane":"subscription", "modelID":"model", "grantID":grant.id, "methodPolicy":proof})
    refresh = call("refresh/acquire", {"connectionID":"shared", "accountID":"account", "expectedRevision":1, "bindingID":binding["bindingID"], "methodPolicy":proof})
    updated = call("refresh/commit", {"leaseID":refresh["leaseID"], "connectionID":"shared", "accountID":"account", "expectedRevision":1, "bindingID":binding["bindingID"], "methodPolicy":proof, "credential":{"type":"oauth", "access":"new", "refresh":"new", "expires":9999999999999}})
    assert updated["credentialRevision"] == 2
    lease = call("credential/lease", {"bindingID":binding["bindingID"], "expectedBindingRevision":binding["bindingRevision"], "expectedCredentialRevision":2})
    assert lease["credential"]["access"] == "new" and lease["methodPolicy"] == proof
    engine.dispose()
