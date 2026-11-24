"""Focused policy CAS against an isolated preferences file."""
import copy

import pytest

from routes import prefs_routes
from src.openclank.memory_policy import MemoryPolicyError, MemoryPolicyStore, defaults


def test_isolated_owner_cas_and_strict_types(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "prefs.json"))
    capability = {"version":"fixture", "axes":{"importance":["rules"], "connections":["rules"], "merge":["rules"]}, "consumers":{}}
    store = MemoryPolicyStore(capability_provider=lambda: capability, graph_provider=lambda: {"state":"available", "value":{"enabled":True, "every_n_turns":3, "api_key":"secret-value", "prompt":"raw-prompt", "extra_flag":True}})
    prefs_routes._update_for_user("alice", {"memory_mode":"manual", "logging_preferences":{"preserve":True}, "theme":"dark"})
    # Logging is protected even when a generic patch attempts to set it.
    assert "logging_preferences" not in prefs_routes._load_for_user("alice")
    before = store.effective_snapshot("alice", read_privilege=True, write_privilege=True, incognito=False, no_memory=False, compare_mode=False)
    assert before["capture_allowed"] and before["capture_mode"] == "manual" and not before["direct_write_allowed"]
    accepted = store.accept("alice", defaults(), expected_revision=0)
    assert accepted["revision"] == 1
    assert store.snapshot("bob")["state"] == "legacy_compatibility"
    with pytest.raises(MemoryPolicyError, match="changed"):
        store.accept("alice", defaults(), expected_revision=0)
    bad = copy.deepcopy(defaults()); bad["budget"]["daily_calls"] = True
    with pytest.raises(MemoryPolicyError):
        store.accept("alice", bad, expected_revision=1)
    prefs_routes._save_for_user("alice", {"theme":"light", "memory_policy":{"revision":999}})
    assert store.snapshot("alice")["revision"] == 1
    assert prefs_routes._load_for_user("alice")["theme"] == "light"
    assert store.effective_snapshot("alice", read_privilege=True, write_privilege=True, incognito=True, no_memory=False, compare_mode=False)["capture_allowed"] is False

    prefs_routes._update_for_user("bob", {"auto_memory":"false", "memory_enabled":"invalid"})
    malformed = store.effective_snapshot("bob", read_privilege=True, write_privilege=True, incognito=False, no_memory=False, compare_mode=False)
    assert malformed["eligibility_state"] == "unavailable" and malformed["eligibility_reason"] == "unsupported_legacy_preferences"
    assert not malformed["read_allowed"] and not malformed["capture_allowed"] and not malformed["direct_write_allowed"]
    assert malformed["capture_mode"] is None and set(malformed["unsupported_inputs"]) == {"auto_memory", "memory_enabled"}
    preview = store.snapshot("bob")["legacy"]
    assert preview["capture_allowed"] is True  # Existing legacy truthiness, reported only.
    assert preview["graph"]["values"] == {"enabled":True, "every_n_turns":3}
    assert preview["graph"]["unknown_fields"] == ["extra_flag"] and preview["graph"]["unknown_fields_present"]
    assert "secret-value" not in str(preview) and "raw-prompt" not in str(preview)


def test_work_kind_config_is_owner_cas_and_does_not_activate(monkeypatch):
    import threading
    from types import SimpleNamespace
    from src.openclank import chat_routing
    data = {}
    monkeypatch.setattr(prefs_routes, 'PREFERENCE_LOCK', threading.RLock())
    monkeypatch.setattr(prefs_routes, '_load_for_user', lambda user: copy.deepcopy(data.get(user, {})))
    monkeypatch.setattr(prefs_routes, '_save_for_user', lambda user, value, **kwargs: data.__setitem__(user, copy.deepcopy(value)))
    route = SimpleNamespace(connection_id='connection', provider_grant_id=None, operations=('chat.complete',),
        option_route_id='option', catalog_revision=7, capabilities={'reasoning_effort':['none','0','high']})
    monkeypatch.setattr(chat_routing, 'resolve_chat_route', lambda **kwargs: route if kwargs.get('model_route_id') == 'route' else (_ for _ in ()).throw(ValueError('bad route')))
    active = {'version':'fixture','axes':{'importance':['rules'],'connections':['rules'],'merge':['rules']},'consumers':{}}
    empty = {'version':'fixture','axes':{'importance':[],'connections':[],'merge':[]},'consumers':{'scheduler':False}}
    capability = [active]
    store = MemoryPolicyStore(capability_provider=lambda: capability[0], graph_provider=lambda: {'state':'unavailable'})
    v1 = store.accept('alice', defaults(), expected_revision=0)
    before = copy.deepcopy(data['alice']['memory_policy'])
    capability[0] = empty
    selection = {'version':1,'modelRouteID':'route','connectionID':'connection','modelID':'model','grantID':None,'optionRouteID':'option','catalogRevision':7,'options':{'reasoning_effort':'0'}}
    result = store.set_work_kind('alice','interpretation',selection,expected_revision=1)
    doc = result['document']
    assert result['state'] == 'accepted' and result['consumer_activation'] == 'inactive'
    assert doc['accepted_policy'] == before == v1 and doc['revision'] == 2
    assert doc['workKindAssignments']['interpretation'] == {'revision':1,'selection':selection}
    assert doc['workKindAssignments']['synthesis']['revision'] == 0
    assert store.set_work_kind('alice','interpretation',selection,expected_revision=2)['revision'] == 2
    with pytest.raises(MemoryPolicyError, match='changed'):
        store.set_work_kind('alice','synthesis',selection,expected_revision=1)
    bad = {**selection,'modelRouteID':'missing'}
    with pytest.raises(MemoryPolicyError): store.set_work_kind('alice','synthesis',bad,expected_revision=2)
    with pytest.raises(MemoryPolicyError): store.set_work_kind('alice','synthesis',{**selection,'options':{'reasoning_effort':'unsupported'}},expected_revision=2)
    default = {**selection, 'options':{}}
    doc = store.set_work_kind('alice','synthesis',default,expected_revision=2)['document']
    assert doc['revision'] == 3 and doc['workKindAssignments']['interpretation']['revision'] == 1
    assert doc['workKindAssignments']['synthesis'] == {'revision':1,'selection':default}
    capability[0] = active
    accepted = store.accept('alice', defaults(), expected_revision=3)
    updated = accepted['document']
    assert accepted['state'] == 'accepted' and accepted['revision'] == 4 and updated['accepted_policy']['revision'] == 2
    assert updated['workKindAssignments'] == doc['workKindAssignments']
    capability[0] = empty
    new = store.set_work_kind('bob','skill_processing',selection,expected_revision=0)
    assert new['state'] == 'legacy_compatibility' and new['document']['accepted_policy'] is None
    assert new['consumer_activation'] == 'inactive'


def test_public_work_kind_shared_projection_and_cas(monkeypatch):
    import threading
    from types import SimpleNamespace
    from src.openclank import chat_routing
    from src.openclank.memory_policy import normalize_public_assignment, public_policy_response
    data = {}
    monkeypatch.setattr(prefs_routes, 'PREFERENCE_LOCK', threading.RLock())
    monkeypatch.setattr(prefs_routes, '_load_for_user', lambda user: copy.deepcopy(data.get(user, {})))
    monkeypatch.setattr(prefs_routes, '_save_for_user', lambda user, value, **kwargs:data.__setitem__(user,copy.deepcopy(value)))
    shared = SimpleNamespace(model_route_id='private-route', connection_id='private-connection', provider_model_id='shared-model',
        provider_grant_id='grant-original', public_endpoint_id='share:grant-original', option_route_id='opaque-option', catalog_revision=4,
        operations=('chat.complete',), capabilities={'reasoning_effort':['none','0']})
    current = [shared]
    monkeypatch.setattr(chat_routing,'list_chat_routes', lambda owner:([], list(current)))
    monkeypatch.setattr(chat_routing,'resolve_chat_route', lambda **kwargs: shared if current else (_ for _ in ()).throw(ValueError('revoked')))
    store = MemoryPolicyStore(graph_provider=lambda:{'state':'unavailable'})
    public = {'version':1,'endpointID':'share:grant-original','modelID':'shared-model','optionRouteID':'opaque-option','catalogRevision':4,'options':{'reasoning_effort':'none'}}
    with pytest.raises(MemoryPolicyError):
        normalize_public_assignment('recipient',public,allowed_models=frozenset())
    internal = normalize_public_assignment('recipient',public,allowed_models=frozenset({'shared-model'}))
    assert internal['grantID'] == 'grant-original' and internal['connectionID'] == 'private-connection'
    saved = store.set_work_kind('recipient','interpretation',internal,expected_revision=0)
    projected = public_policy_response('recipient',saved)
    assert projected['document']['workKindAssignments']['interpretation']['selection'] == {'state':'available', **public}
    assert 'private-connection' not in str(projected) and 'private-route' not in str(projected)
    denied_projection = public_policy_response('recipient',saved,allowed_models=frozenset())
    assert denied_projection['document']['workKindAssignments']['interpretation']['selection']['state'] == 'unavailable'
    assert 'private-connection' not in str(denied_projection) and 'private-route' not in str(denied_projection)
    assert public_policy_response('recipient',saved['document'])['workKindAssignments']['interpretation']['selection'] == {'state':'available', **public}
    with pytest.raises(MemoryPolicyError,match='changed'):
        store.set_work_kind('recipient','interpretation',internal,expected_revision=0)
    default = {**public,'options':{}}
    second = store.set_work_kind('recipient','synthesis',normalize_public_assignment('recipient',default),expected_revision=1)
    assert public_policy_response('recipient',second)['document']['workKindAssignments']['synthesis']['selection']['options'] == {}
    current.clear()
    unavailable = public_policy_response('recipient',second)['document']['workKindAssignments']['interpretation']['selection']
    assert unavailable == {'state':'unavailable','optionRouteID':'opaque-option','catalogRevision':4,'options':{'reasoning_effort':'none'}}
    assert store.snapshot('recipient')['document']['workKindAssignments']['interpretation']['selection']['grantID'] == 'grant-original'
    with pytest.raises(MemoryPolicyError): normalize_public_assignment('recipient', public)
