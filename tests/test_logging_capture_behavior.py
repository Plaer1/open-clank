"""Disposable managed logging qualification; no live app bootstrap/provider calls."""
import os
from pathlib import Path
import tempfile

# This entrypoint is safe even with pytest --noconftest: establish disposable
# authority BEFORE core.database or anything which may import its initializer.
_fixture = Path(tempfile.mkdtemp(prefix="openclank-capture-probe-"))
for _key, _value in {"OPEN_CLANK_DATA_DIR": str(_fixture / "data"),
                     "DATABASE_URL": "sqlite:///" + str(_fixture / "initializer.sqlite3"),
                     "FM_DB_PATH": str(_fixture / "fm.sqlite3")}.items():
    os.environ[_key] = _value

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base
from core.stats_models import StatsEvent
from services.stats.ledger import capture_operation_result, project_admitted_events
from src.openclank.logging_models import LoggingAttemptDetail, LoggingCaptureBody, LoggingCaptureEvent
from src.openclank.logging_policy import LoggingPolicyStore
from src.openclank.logging_capture_store import LoggingCaptureStore, CaptureError
from src.openclank.sql_owner_lifecycle import CommonSqlOwnerLifecycle


def test_actor_final_evidence_does_not_gate_execution_finish(tmp_path):
    from core.provider_models import ProviderBase, ProviderOperationBinding
    from src.openclank.acp_bridge import ACPBridge
    from src.openclank.provider_callbacks import ManagedProviderCallbacks
    engine=create_engine('sqlite:///' + str(tmp_path / 'actor-final.sqlite3'))
    Base.metadata.create_all(engine);ProviderBase.metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    identity={'sessionID':'engine','rootOperationID':'root','actorID':'child','executionRevision':1}
    binding=ProviderOperationBinding(id='binding',owner='alice',credential_owner='alice',root_operation_id='root',
        engine_session_id='engine',actor_id='child',execution_revision=1,connection_id='connection',provider_id='provider',
        billing_lane='metered_api',model_id='model',model_route_id='route',selected_account_id='account',revision=1)
    db=factory();db.add(binding);db.commit();db.close()
    policy=LoggingPolicyStore(loader=lambda _owner:{},saver=lambda *_:None)
    store=LoggingCaptureStore(factory,policy_store=policy)
    snapshot={'version':1,'revision':1,'advanced_enabled':False,'request_body_enabled':True,
              'response_body_enabled':True,'binary_body_enabled':False,'private':False}
    context={'owner':'alice','holder_id':'worker','root_operation_id':'root','operation_id':'actor-binding:binding',
        'actor_id':'child','actor_kind':'subagent','actor_execution':identity,'logging_policy_snapshot':snapshot}
    store.admit(owner='alice',holder_id='worker',context=context,binding=binding)
    callbacks=ManagedProviderCallbacks.__new__(ManagedProviderCallbacks)
    callbacks._owner='alice';callbacks._holder_id='worker';callbacks._capture=store;callbacks._session_factory=factory
    class Runtime:
        def __init__(self): self.left=0
        def leave(self,_handle): self.left+=1
    runtime=Runtime();bridge=ACPBridge.__new__(ACPBridge)
    bridge._managed_execution_lock=asyncio.Lock();bridge._managed_finished_executions=set();bridge._managed_logging_uncertain=set()
    bridge._managed_actor_turns={};bridge._managed_actor_executions={('engine','root','child',1):{'foregroundRuntime':runtime,'foregroundHandle':'handle'}}
    bridge._logging_callbacks=callbacks
    async def drain(callback): return callback()
    bridge._drain_actor_logging=drain
    evidence={'version':1,'bindingID':'binding','bindingRevision':1,'outcome':'success','usage':{'inputTokens':20,'audioInputTokens':4},
        'metricCoverage':{'input_tokens':{'state':'reported','coverage':'complete','source':'sdk'}},
        'coveredDispatchIDs':['dispatch-1','dispatch-2'],'lossReasons':[],'identityCoverage':'complete','normalizationProfile':'separated-v1'}
    async def exercise():
        with pytest.raises(ValueError): await bridge._handle_managed_execution_finish({**identity,'unknownBase':True})
        result=await bridge._handle_managed_execution_finish({**identity,'evidence':evidence})
        assert result == {'accepted':True,'replayed':False,'captureState':'recorded'}
        replay=await bridge._handle_managed_execution_finish({**identity,'evidence':evidence})
        assert replay['accepted'] and replay['replayed'] and replay['captureState']=='unavailable'
        invalid={**identity,'actorID':'other','evidence':{'unexpected':'payload'}}
        bridge._managed_actor_executions[('engine','root','other',1)]={'foregroundRuntime':runtime,'foregroundHandle':'handle'}
        result=await bridge._handle_managed_execution_finish(invalid)
        assert result == {'accepted':True,'replayed':False,'captureState':'unavailable'}
    asyncio.run(exercise())
    db=factory();events=db.query(StatsEvent).filter_by(owner='alice',operation_id='actor-binding:binding').all()
    assert len(events)==1 and events[0].input_tokens==20 and events[0].event_metadata['extra_metrics']=={'audioInputTokens':4} and runtime.left==2
    db.close();engine.dispose()


def test_uncertain_actor_capture_fences_only_its_scope():
    from src.openclank.logging_capture_store import ActorCaptureFenced
    policy=LoggingPolicyStore(loader=lambda _owner:{},saver=lambda *_:None)
    store=LoggingCaptureStore(lambda:None,policy_store=policy)
    snapshot={'version':1,'revision':1,'advanced_enabled':False,'request_body_enabled':True,
              'response_body_enabled':True,'binary_body_enabled':False,'private':False}
    identity={'sessionID':'engine','rootOperationID':'root','actorID':'child','executionRevision':1}
    context={'owner':'alice','holder_id':'worker','root_operation_id':'root','operation_id':'actor-binding:binding',
             'actor_id':'child','actor_kind':'subagent','actor_execution':identity,'logging_policy_snapshot':snapshot}
    binding=SimpleNamespace(id='binding',revision=1,root_operation_id='root',provider_id='provider',connection_id='connection',
        selected_account_id='account',billing_lane='metered_api',model_id='model',model_route_id='route')
    token=store.admit(owner='alice',holder_id='worker',context=context,binding=binding)['admissionID']
    store.fence_actor_execution('alice','worker',identity)
    assert not store.events(owner='alice',holder_id='worker',payload={'admissionID':token,'dispatchID':'late',
        'sequence':1,'source':'wire'})['accepted']
    with pytest.raises(ActorCaptureFenced):
        store.admit(owner='alice',holder_id='worker',context=context,binding=binding)
    other={**identity,'actorID':'other'}
    other_context={**context,'actor_id':'other','actor_execution':other,'operation_id':'actor-binding:other'}
    other_binding=SimpleNamespace(**{**binding.__dict__,'id':'other'})
    assert store.admit(owner='alice',holder_id='worker',context=other_context,binding=other_binding)['admissionID']


def test_actor_capture_root_two_children_retry_once(tmp_path):
    from services.stats.ledger import capture_chat_metrics
    from services.stats.query import StatsScope, query_summary
    engine = create_engine('sqlite:///' + str(tmp_path / 'actor-stats.sqlite3'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine,expire_on_commit=False)
    policy = LoggingPolicyStore(loader=lambda _owner:{},saver=lambda *_:None)
    store = LoggingCaptureStore(factory,policy_store=policy)
    snapshot = {'version':1,'revision':1,'advanced_enabled':False,'request_body_enabled':True,
                'response_body_enabled':True,'binary_body_enabled':False,'private':False}
    def admission(actor,op,connection,root='root'):
        binding = SimpleNamespace(id='binding-'+actor,revision=1,root_operation_id=root,provider_id='provider',
            connection_id=connection,selected_account_id='account',billing_lane='metered_api',model_id='model',model_route_id='route')
        identity = {'sessionID':'engine','rootOperationID':root,'actorID':actor,'executionRevision':1}
        context = {'owner':'alice','holder_id':'worker','root_operation_id':root,'operation_id':op,
                   'actor_id':actor,'actor_kind':'foreground' if actor=='main' else 'subagent',
                   'actor_execution':identity,'logging_policy_snapshot':snapshot}
        return identity,store.admit(owner='alice',holder_id='worker',context=context,binding=binding)['admissionID']
    main,main_token=admission('main','root-op','connection')
    child1,child1_token=admission('child1','actor-binding:binding-child1','connection')
    child2,child2_token=admission('child2','actor-binding:binding-child2','different-connection')
    def emit(token,dispatch,tokens,retry=None):
        payload={'admissionID':token,'dispatchID':dispatch,'sequence':1,'source':'wire','terminal':True,
                 'outcome':'completed','metrics':{'input_tokens':tokens},'normalizationProfile':'separated-v1'}
        if retry: payload['retryOfDispatchID']=retry
        assert store.events(owner='alice',holder_id='worker',payload=payload)['accepted']
    emit(main_token,'main-1',10)
    emit(main_token,'main-2',3,'main-1')
    store.finish_actor_execution('alice','worker',main)
    store.finish_operation('alice','worker','root')
    # Both admitted children remain live even after the foreground root ends.
    emit(child1_token,'child-1',20)
    emit(child2_token,'child-2',30)
    store.finish_actor_execution('alice','worker',child1)
    store.finish_actor_execution('alice','worker',child2)
    db=factory()
    capture_chat_metrics(db,owner='alice',context={'owner':'alice','root_operation_id':'root',
        'operation_id':'root-op','instance_id':'worker'},metrics={'input_tokens':13,
        'covered_dispatch_ids':['main-1','main-2'],'metric_coverage':{'input_tokens':{'state':'reported','coverage':'complete','source':'sdk'}}})
    db.commit()
    report=query_summary(db,StatsScope('alice','UTC',None,None,'day'))
    assert report['totals']['input_tokens']['value']==63
    assert report['operation_counts']['dispatch_attempts']==4
    assert db.query(StatsEvent).filter_by(owner='alice',actor_kind='subagent').count()==2
    # An aggregate with no proven dispatch coverage is never added to its
    # overlapping attempt as if both were independent measurements.
    _unknown,unknown_token=admission('unknown','unknown-op','connection',root='unlinked')
    emit(unknown_token,'unknown-1',5)
    capture_chat_metrics(db,owner='alice',context={'owner':'alice','root_operation_id':'unlinked',
        'operation_id':'unknown-op','instance_id':'worker'},metrics={'input_tokens':7,'covered_dispatch_ids':[]})
    db.commit()
    ambiguous=query_summary(db,StatsScope('alice','UTC',None,None,'day'))
    assert ambiguous['totals']['input_tokens']['value']==70
    assert ambiguous['coverage']['state']=='partial_ambiguous'
    db.close();engine.dispose()


def test_provider_attempt_detail_uses_owned_binding_after_rotation(tmp_path):
    from core.provider_models import ProviderBase, ProviderOperationBinding
    from src.openclank.logging_models import LoggingBase

    engine = create_engine("sqlite://")
    ProviderBase.metadata.create_all(engine)
    LoggingBase.metadata.create_all(engine)
    # The synthetic binding represents a previously admitted operation;
    # unrelated connection/account rows are intentionally outside this fixture.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    policy = LoggingPolicyStore(loader=lambda _owner: {}, saver=lambda *_args: None)
    store = LoggingCaptureStore(factory, policy_store=policy)
    binding = ProviderOperationBinding(id="binding", owner="alice", credential_owner="source-owner", root_operation_id="root",
        connection_id="connection", provider_id="openai", billing_lane="api", model_id="model", model_route_id="route",
        selected_account_id="rotated-account", revision=3, attempts=[{"attempt": 1, "outcome": "transient", "account_id": "old-account",
            "evidence": {"version": 1, "kind": "throttle", "httpStatus": 429, "localNotBefore": "2026-01-01T00:00:00Z"}}])
    db = factory()
    db.add(binding)
    db.commit()
    context = {"owner": "alice", "holder_id": "holder", "root_operation_id": "root", "operation_id": "operation"}
    admission = store.admit(owner="alice", holder_id="holder", context=context, binding=binding)
    captured = dict(store.admissions[admission["admissionID"]]["context"])
    captured["account_id"] = "old-account"  # Binding has since rotated.
    db.add(LoggingAttemptDetail(id="owned", owner="alice", instance_id="holder", operation_id="operation", attempt_id="dispatch",
        root_operation_id="root", policy_revision=1, attribution=captured, capture_state="ready", timing={}, loss_reasons=[]))
    db.add(LoggingAttemptDetail(id="foreign", owner="bob", instance_id="holder", operation_id="other", attempt_id="dispatch",
        root_operation_id="root", policy_revision=1, attribution=captured, capture_state="ready", timing={}, loss_reasons=[]))
    db.commit()
    db.close()
    result = store.attempt("alice", "owned")["resource"]["provider_attempts"]
    assert result == {"state": "reported", "scope": "operation", "truncated": False, "items": [{"attempt": 1,
        "outcome": "transient", "evidence": {"version": 1, "kind": "throttle", "httpStatus": 429, "localNotBefore": "2026-01-01T00:00:00Z"}}]}
    assert "account" not in json.dumps(result)
    assert store.attempt("bob", "foreign")["resource"]["provider_attempts"]["state"] == "unavailable"
    with pytest.raises(CaptureError):
        store.attempt("bob", "owned")
    outer='11111111-1111-4111-8111-111111111111'; inner='22222222-2222-4222-8222-222222222222'
    observation={'version':1,'profile':'openai.codex.responses.v1','captureDispatchID':outer,'upstreamDispatchID':inner,'httpStatus':429}
    db=factory(); stored=db.query(ProviderOperationBinding).filter_by(id='binding').one()
    stored.attempts=[{**stored.attempts[0],'dispatch_observation':observation}]
    db.query(LoggingAttemptDetail).filter_by(id='owned').one().attempt_id=outer
    db.commit();db.close()
    matched=store.attempt('alice','owned')['resource']
    assert matched['provider_attempts']['dispatch_match']=={'state':'exact','attempt':1}
    assert matched['provider_attempts']['items'][0]['dispatchObservation']==observation
    assert matched['measurement']['provider_attempt']=={'state':'unavailable'}  # no Stats row was persisted
    db=factory();stored=db.query(ProviderOperationBinding).filter_by(id='binding').one()
    stored.attempts=[stored.attempts[0],{**stored.attempts[0],'attempt':2}];db.commit();db.close()
    assert store.attempt('alice','owned')['resource']['provider_attempts']['dispatch_match']=={'state':'unavailable'}
    engine.dispose()


def test_managed_capture_policy_gap_retention_and_owner_lifecycle(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "captures.sqlite3"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    preferences = {}
    policy = LoggingPolicyStore(loader=lambda owner: preferences.get(owner, {}), saver=lambda owner, value: preferences.__setitem__(owner, value))
    policy.update_policy("a", {"advanced_enabled": True}, expected_revision=1)
    store = LoggingCaptureStore(factory, policy_store=policy)
    context = {"owner": "a", "holder_id": "worker", "root_operation_id": "root", "operation_id": "operation", "operation_type": "chat"}
    binding = SimpleNamespace(root_operation_id="root", provider_id="openai", connection_id="connection", selected_account_id="account", billing_lane="api", model_id="selected-model")
    admission = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="attempt", wire_format="responses.v1")
    payload = {"admissionID": admission["admissionID"], "sequence": 1, "terminal": True, "outcome": "completed", "metrics": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5}, "actualModel": "returned-model", "timing": {"terminal_ms": 12.6},
               "requestBody": {"password": "must disappear", "source": {"type": "base64", "data": "YWJj"}}, "responseBody": {"content": "hello"}, "events": [{"text": "hello"}]}
    with pytest.raises(CaptureError):
        store.events(owner="b", holder_id="worker", payload=payload)
    assert store.events(owner="a", holder_id="worker", payload=payload)["accepted"]
    assert store.events(owner="a", holder_id="worker", payload=payload)["replayed"]
    listing = store.attempts("a", limit=1)
    handle = listing["items"][0]["handle"]
    resource = store.attempt("a", handle, include_bodies=True)["resource"]
    assert resource["actual_model"] == "returned-model" and resource["requested_model"] == "selected-model"
    request = json.loads(next(body["text"] for body in resource["bodies"] if body["category"] == "request"))
    assert "password" not in request and request["source"]["data"]["omitted"] == "embedded_media"
    db = factory()
    before = project_admitted_events(db.query(StatsEvent).filter_by(owner="a").all())
    assert sum((row.input_tokens or 0)+(row.output_tokens or 0) for row in before.events) == 120
    # Actual SDK-operation producer observation reconciles with wire evidence.
    sdk = SimpleNamespace(state="complete", operation_id="operation", root_operation_id="root", model_route_id="route", selected_account_id="account", billing_lane="metered_api", normalization_profile="openai-wire-inclusive-v1", model_fingerprint=None, usage={"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5})
    capture_operation_result(db, SimpleNamespace(owner="a"), sdk)
    db.commit()
    reconciled = project_admitted_events(db.query(StatsEvent).filter_by(owner="a").all())
    assert sum((row.input_tokens or 0)+(row.output_tokens or 0) for row in reconciled.events) == 120
    assert len(reconciled.excluded) == 1
    db.close()
    store.finish_operation("a", "worker", "root")
    # A missing capture terminal must not invent a provider cancellation.
    gap = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="gap", wire_format="responses.v1")
    store.events(owner="a", holder_id="worker", payload={"admissionID": gap["admissionID"], "sequence": 0})
    store.finish_operation("a", "worker", "root")
    db = factory()
    assert db.query(StatsEvent).filter_by(owner="a", event_kind="cancellation").count() == 0
    gap_row = db.query(LoggingAttemptDetail).filter_by(attempt_id="gap").one()
    assert gap_row.outcome is None and gap_row.capture_state == "dropped"
    assert not store.admissions
    # Body-OFF still retains numeric usage but cannot persist raw event text.
    policy.update_policy("a", {"response_body_enabled": False, "binary_body_enabled": True}, expected_revision=2)
    third = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="body-off", wire_format="responses.v1")
    store.events(owner="a", holder_id="worker", payload={**payload, "admissionID": third["admissionID"], "requestBody": {"source": {"type": "base64", "data": "YWJj"}}})
    third_id = store.admissions[third["admissionID"]]["detail_id"]
    assert db.query(LoggingCaptureEvent).filter_by(attempt_detail_id=third_id).count() == 0
    assert db.query(LoggingCaptureBody).filter_by(attempt_detail_id=third_id, category="response").count() == 0
    third_resource = store.attempt("a", third_id, include_bodies=True)["resource"]
    assert "YWJj" in third_resource["bodies"][0]["text"]
    db.expire_all()
    distinct = project_admitted_events(db.query(StatsEvent).filter_by(owner="a").all())
    assert sum((row.input_tokens or 0)+(row.output_tokens or 0) for row in distinct.events) == 240
    assert len({row.attempt_id for row in distinct.events if row.attempt_id}) == 3
    # Disable new operation admissions while existing immutable policy drains.
    policy.update_policy("a", {"ordinary_enabled": False}, expected_revision=3)
    assert policy.logging_status("a")["draining"]
    drained = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="drain", wire_format="responses.v1")
    assert drained["admissionID"]
    new_binding = SimpleNamespace(**{**vars(binding), "root_operation_id": "new-root"})
    fresh = store.admit(owner="a", holder_id="worker", context={**context, "root_operation_id": "new-root", "operation_id": "new-operation"}, binding=new_binding, attempt_id="new", wire_format="responses.v1")
    assert fresh["admissionID"] == ""
    # Host typed-operation incognito propagation reaches admission before call.
    from src.openclank.mimo_supervisor import MimoSupervisor
    class Callbacks:
        def register_logging_operation(self, root, supplied):
            private = store.admit(owner="a", holder_id="worker", context={**context, **supplied, "root_operation_id": root}, binding=binding, attempt_id="private", wire_format="responses.v1")
            assert private["admissionID"] == "" and not private["policy"]["ordinary_enabled"]
        def finish_logging_operation(self, root):
            pass
    class Client:
        async def execute_managed_operation(self, payload):
            return {"ok": True}
    supervisor = SimpleNamespace(_client=Client(), _managed_callbacks=Callbacks(), is_alive=lambda: True)
    assert asyncio.run(MimoSupervisor.execute_operation(supervisor, {"rootOperationID": "root", "operation": "text.generate", "options": {"incognito": True}}))["ok"]
    # Optional raw-detail failure is isolated from already-admitted usage.
    failed_detail = store.admit(owner="a", holder_id="worker", context=context, binding=binding, attempt_id="detail-failure", wire_format="responses.v1")
    missing_id = store.admissions[failed_detail["admissionID"]]["detail_id"]
    db.query(LoggingAttemptDetail).filter_by(id=missing_id).delete()
    db.commit()
    assert store.events(owner="a", holder_id="worker", payload={**payload, "admissionID": failed_detail["admissionID"]})["accepted"]
    assert store.status("a")["losses"]["write_failed"] >= 1
    store.recover_generation("a", "next-worker")
    assert store.attempt("a", store.admissions[drained["admissionID"]]["detail_id"])["resource"]["capture_state"] == "dropped"
    from src.openclank.logging_models import recover_pending_captures
    db.add(LoggingAttemptDetail(id="unclean-restart", owner="a", instance_id="old-worker", operation_id="past", attempt_id="past", root_operation_id="past", policy_revision=1, attribution={}, loss_reasons=["truncated"]))
    db.commit()
    assert recover_pending_captures(engine) == 1
    db.expire_all()
    restarted = db.query(LoggingAttemptDetail).filter_by(id="unclean-restart").one()
    assert restarted.capture_state == "dropped" and restarted.outcome is None and restarted.loss_reasons == ["truncated", "interrupted"]
    assert db.query(LoggingAttemptDetail).filter_by(id=handle).one().capture_state == "complete"
    fixture_path = os.environ.get("LOGGING_UI_FIXTURE_PATH")
    if fixture_path:
        resources = [store.attempt("a", item["handle"], include_bodies=True) for item in store.attempts("a")["items"]]
        Path(fixture_path).write_text(json.dumps({"attempts": store.attempts("a"), "resources": resources}, indent=2))
    preview = store.prune(owner="a", action="preview", payload={"target": "advanced_bodies", "retention": {"mode": "size", "max_bytes": 1}})
    assert preview["preview"]["owner_body_bytes"] > 0
    applied = store.prune(owner="a", action="apply", payload={"target": "advanced_bodies", "preview_id": preview["preview_id"]})
    assert applied["applied"]["body_bytes"] > 0
    assert db.query(StatsEvent).filter_by(owner="a").count() == 5
    db.close()
    lifecycle = CommonSqlOwnerLifecycle(factory)
    assert any(binding.model is LoggingAttemptDetail for binding in lifecycle.bindings)
    lifecycle.rename_owner("a", "renamed")
    assert store.attempt("renamed", handle)["resource"]["handle"] == handle
    with pytest.raises(CaptureError):
        store.attempt("a", handle)
    lifecycle.purge_owner("renamed")
    db = factory()
    assert db.query(LoggingAttemptDetail).count() == 0
    assert db.query(LoggingCaptureBody).count() == 0
    assert db.query(LoggingCaptureEvent).count() == 0
    db.close()
    # Shared preference authority merges a concurrent ordinary key with policy.
    import routes.prefs_routes as prefs
    prefs.PREFS_FILE = str(tmp_path / "prefs.json")
    real = LoggingPolicyStore()
    with ThreadPoolExecutor(max_workers=2) as pool:
        logging = pool.submit(real.update_policy, "owner", {"advanced_enabled": True}, expected_revision=1)
        ordinary = pool.submit(prefs._update_for_user, "owner", {"theme": "dark"})
        logging.result(); ordinary.result()
    saved = prefs._load_for_user("owner")
    assert saved["theme"] == "dark" and saved["logging_preferences"]["advanced_enabled"]


def test_current_main_context_requires_exact_receipt_and_binding(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.provider_models import ProviderBase, ProviderOperationBinding
    from services.stats.ledger import capture_context_receipt, capture_attempt_event
    from routes.history import history_routes
    from src import model_dispatch
    from src.openclank import acp_supervisor_adapter
    engine = create_engine('sqlite:///' + str(tmp_path / 'context.sqlite3'))
    Base.metadata.create_all(engine); ProviderBase.metadata.create_all(engine)
    with engine.connect() as conn: conn.exec_driver_sql('PRAGMA foreign_keys=OFF')
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    identity = {'sessionID':'engine','rootOperationID':'root','actorID':'main','executionRevision':1}
    binding = ProviderOperationBinding(id='binding',owner='alice',credential_owner='alice',root_operation_id='root',engine_session_id='engine',actor_id='main',execution_revision=1,connection_id='connection',provider_id='provider',billing_lane='metered_api',model_id='model',model_route_id='route',revision=1)
    db = factory(); db.add(binding); db.flush()
    ctx = {'actor_kind':'foreground','mapping_revision':2,'instance_id':'holder','session_id':'chat','operation_id':'op','root_operation_id':'root'}
    capture_context_receipt(db,owner='alice',context=ctx,identity=identity,binding=binding,receipt={'dispatchID':'d','complete':True})
    capture_attempt_event(db,owner='alice',context=ctx,attempt_id='d',payload={'source':'sdk','sequence':1,'terminal':True,'outcome':'completed','metrics':{'input_tokens':100,'cache_read_tokens':40},'metric_coverage':{'input_tokens':{'state':'reported','coverage':'complete','source':'sdk'}},'normalization_profile':'managed-sdk-inclusive-v1','covered_dispatch_ids':['d'],'identity_coverage':'complete'})
    db.commit(); db.close()
    class Mapping:
        def lookup(self, chat): return {'owner':'alice','current':'engine','revision':2}
    bridge=SimpleNamespace(_durable_session_map=Mapping(),_session_state={'engine':{'last_root_operation_id':'root'}},_session_context={'engine':{'incognito':False,'auxiliary':False}},_managed_provider_generation='holder')
    worker=SimpleNamespace(_bridge=bridge,is_alive=lambda:True)
    class Adapter:
        def owner_lifecycle_pool(self): return SimpleNamespace(worker_for_owner=lambda owner:worker)
    monkeypatch.setattr(acp_supervisor_adapter,'AcpSupervisorAdapter',Adapter)
    monkeypatch.setattr(model_dispatch,'get_agent_supervisor',lambda:Adapter())
    monkeypatch.setattr(history_routes,'SessionLocal',factory)
    assert history_routes._last_proven_main_context('alice','chat')['tokens']==100
    db=factory(); db.query(ProviderOperationBinding).filter_by(id='binding').update({'revision':2}); db.commit(); db.close()
    assert history_routes._last_proven_main_context('alice','chat') is None
    engine.dispose()


def test_host_stage_timing_is_distinct_from_dispatch_clock(tmp_path):
    from services.stats.ledger import capture_chat_metrics, project_admitted_events
    engine = create_engine('sqlite:///' + str(tmp_path / 'host-timing.sqlite3'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db=factory()
    context={'owner':'alice','instance_id':'host','root_operation_id':'root','operation_id':'op'}
    event=capture_chat_metrics(db,owner='alice',context=context,metrics={'inputTokens':7,'host_stage_timing':{'version':1,'clock':'host_monotonic','chat_transcript_snapshot_ms':12.4,'preparation_ms':5,'result_processing_ms':3}},outcome='completed')
    db.commit()
    timing=event.event_metadata['host_stage_timing']
    assert timing == {'version':1,'clock':'host_monotonic','chat_transcript_snapshot_ms':12,'preparation_ms':5,'result_processing_ms':3}
    assert project_admitted_events([event]).events[0].event_metadata['host_stage_timing']==timing
    db.close();engine.dispose()
