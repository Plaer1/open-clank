"""Host half of the optional cross-model fallback chain, end to end.

The engine is played by this test: it calls the Host callbacks in the order the
engine would (bind -> first credential -> 401 terminal -> issue alternative ->
dispatch check -> alternative commit) against a disposable SQLite
provider store with synthetic accounts.  No engine process, no network.
Stubbed: auth manager, barrier credential capture, worker/pool/supervisor,
actor tree reads.  Everything in provider_store / provider_callbacks is real.
Run with pytest.
"""
import asyncio, atexit, json, os, secrets, shutil, sys, tempfile, threading, types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = Path(tempfile.mkdtemp(prefix='openclank-b6c-')).resolve()
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)
for _n, _p in {'OPEN_CLANK_DATA_DIR': 'data', 'FM_DB_PATH': 'fm.sqlite',
               'XDG_CONFIG_HOME': 'config', 'XDG_DATA_HOME': 'xdg-data', 'XDG_CACHE_HOME': 'cache',
               'XDG_STATE_HOME': 'state'}.items():
    os.environ[_n] = str(ROOT / _p)
os.environ['DATABASE_URL'] = 'sqlite:///' + str(ROOT / 'app.sqlite')
sys.path.insert(0, str(REPO))
try:
    import bs4  # noqa: F401
except ImportError:
    _bs4 = types.ModuleType('bs4'); _bs4.BeautifulSoup = object; sys.modules['bs4'] = _bs4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.provider_models import ProviderBase, ProviderOperationBinding
from core.database import Session as DbSession
from src import secret_storage
from src.openclank.acp_bridge import ACPBridge, _TurnState
from src.openclank.account_request_barrier import AccountRequestBarrier, _current_admission
from src.openclank.acp_supervisor_adapter import AcpRuntimeAdapter, AcpRuntimeLease
from src.openclank.chat_routing import _build_owned
from src.openclank.mimo_supervisor import AgentWorkerLease
from src.openclank.provider_callbacks import ManagedProviderCallbacks
from src.openclank.provider_human_policy import (_captured_provider_policy_intent, capture_human_provider_policy,
                                                 run_human_provider_policy)
from src.openclank.provider_store import ProviderStore
from src.openclank.provider_cross_model_schema import MARKER, TRIGGERS

PROFILE = 'anthropic.messages.401-auth-no-output.v1'
P = '_openclank/provider-store/v1/'
POLICY = {'version': 1, 'enabled': True, 'revision': 1,
          'alternatives': [{'route_id': 'route-alt', 'options': {'route_id': 'route-alt', 'catalog_revision': 1, 'options': {}}}]}


def _build_store(tag):
    """Seed a disposable store with the public v2 cross-model writer guards."""
    secret_storage._KEY_PATH = ROOT / 'fixture-key'; secret_storage._fernet = None
    path = ROOT / (tag + '.sqlite')
    engine = create_engine('sqlite:///' + str(path), connect_args={'check_same_thread': False})
    ProviderBase.metadata.create_all(engine)
    DbSession.__table__.create(engine, checkfirst=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE provider_model_routes ADD COLUMN cross_model_policy JSON")
        connection.exec_driver_sql("ALTER TABLE provider_operation_bindings ADD COLUMN cross_model_coordinator JSON")
        connection.exec_driver_sql(MARKER)
        for trigger in TRIGGERS.values():
            connection.exec_driver_sql(trigger)
    factory = sessionmaker(bind=engine, autoflush=False)
    store = ProviderStore(factory)
    for name in ('primary', 'alt'):
        store.create_connection(owner='alice', connection_id='conn-' + name, family_id='anthropic', adapter_id='anthropic-messages',
                                kind='official', billing_lane='metered_api', label=name)
        account = store.create_account(owner='alice', connection_id='conn-' + name, account_id='acct-' + name, label=name,
                                       auth_method='api_key', acquisition_method='manual-key', auth_class='metered',
                                       credentials={'type': 'api', 'key': 'fake-' + secrets.token_hex(8)})
        route = store.create_model_route(owner='alice', connection_id='conn-' + name, model_route_id='route-' + name,
                                         provider_model_id=name, capabilities={'reasoning_effort': ['low']}, operations=['chat.stream'])
        store.set_entitlement(owner='alice', account_id=account.id, model_route_id=route.id, eligible=True)
    with engine.begin() as c:
        c.execute(DbSession.__table__.insert().values(id='chat', name='fixture', endpoint_url='fixture', endpoint_id='conn-primary',
                                                      provider_model_route_id='route-primary', model='primary', owner='alice'))
    return engine, factory, store


class _Auth:
    users = {'alice': {}}
    def account_id(self, owner): return 'host-' + owner if owner == 'alice' else None
    def username_for_account_id(self, account): return 'alice' if account == 'host-alice' else None


class _Request:
    def __init__(self, state):
        self.state = types.SimpleNamespace(authenticated=True, current_user='alice')
        self.app = types.SimpleNamespace(state=state)


class _Env:
    pass


async def _setup(tag, *, policy_on):
    """Real store + callbacks + bridge, driven to the point the engine would call account/bind."""
    e = _Env(); e.engine, factory, e.store = _build_store(tag)
    store = e.store
    barrier = AccountRequestBarrier(lambda _: False)
    state = types.SimpleNamespace(auth_manager=_Auth(), account_request_barrier=barrier, provider_store=store,
                                  capture_original_credential=lambda request, owner, account: (lambda: True))
    class Worker:
        installed_generation = 3; installed_fingerprint = 'fixture'; _proc = object()
        def is_alive(self): return True
    worker = Worker()
    class Pool:
        _s05_foreground_runtime = None
        _states = {'alice': types.SimpleNamespace(active=worker, generation=3, status='ready')}
        def _key(self, owner): return owner
        def _owner_lifecycle_epoch(self, owner): return 2
        def _assert_owner_lifecycle(self, owner, epoch):
            if epoch != 2: raise ValueError('epoch changed')
        def worker_for_owner(self, owner): return worker
        async def _release_lease(self, *a, **k): pass
    pool = Pool()
    class Supervisor:
        _s05_foreground_runtime = None
        def owner_lifecycle_pool(self): return pool
        async def admit_agent(self, owner, provider, model):
            return AcpRuntimeLease(AgentWorkerLease(pool, owner, worker, owner_epoch=2), AcpRuntimeAdapter(worker))
    state.mimo_supervisor = Supervisor()
    e.callbacks = callbacks = ManagedProviderCallbacks(owner='alice', holder_id='holder', store=store, session_factory=factory,
                                                      artifact_store=object(), capture_store=object(), operation_journal=object())
    e.bridge = bridge = ACPBridge.__new__(ACPBridge)
    worker._bridge = bridge; bridge._managed_root_actor_reader = None
    bridge._owner = 'alice'; bridge._managed_execution_lock = asyncio.Lock(); bridge._provider_root_sync_guard = threading.Lock()
    bridge._managed_provider_generation = 'holder'; bridge._managed_provider_context = callbacks.dispatch
    bridge._direct_foreground_attachments = {}; bridge._turns = {}; bridge._prepared_managed_handoffs = {}
    bridge._managed_actor_executions = {}; bridge._managed_actor_turns = {}; bridge._managed_finished_executions = set()
    bridge._session_map = {'chat': 'session'}
    bridge._session_context = {'session': {'owner': 'alice', 'odysseus_session_id': 'chat', 'root_operation_id': 'root'}}
    async def binding_read(params):
        return {'stableChatID': 'chat', 'owner': 'alice', 'authorityWorkspaceID': 'ws', 'mapRevision': 1, 'mappingRevision': 1, 'workspaceRevision': 1}
    bridge._handle_session_binding_read = binding_read
    callbacks.set_actor_execution_context_resolver(bridge.resolve_managed_actor_execution)
    import core.models
    core.models.get_session_manager_instance = lambda: types.SimpleNamespace(lifecycle_context=state)

    # Host capture (chat_routes.py:2418 equivalent): owner saves the policy, then the chat captures the selection.
    original = await barrier.acquire('alice'); e.token = _current_admission.set(original); e.barrier, e.original = barrier, original
    human = capture_human_provider_policy(_Request(state), store)
    def route():
        return _build_owned(store, owner='alice', connection_id='conn-primary', route=store.get_model_route(owner='alice', model_route_id='route-primary'))
    candidate = {'route_id': 'route-primary', 'catalog_revision': route().catalog_revision, 'options': {},
                 'provider_model_route_id': 'route-primary', 'provider_model_id': 'primary'}
    e.saved = None
    if policy_on:
        e.saved = await run_human_provider_policy(human, lambda h: store.set_human_cross_model_policy(h, 'route-primary', 1, POLICY))
    selection = await run_human_provider_policy(human, lambda h: store.capture_human_cross_model_selection(h, 'chat', route(), candidate))
    assert (selection is None) == (not policy_on)
    intent = _captured_provider_policy_intent(human, selection)
    snap = {'version': 1, 'routeID': 'route-primary', 'optionRouteID': 'route-primary', 'catalogRevision': route().catalog_revision,
            'model': {'providerID': 'conn-primary', 'modelID': 'primary'}, 'options': {}, 'source': {'kind': 'default'}}
    direct = await barrier.acquire('alice')
    attachment = await (await state.mimo_supervisor.admit_agent('alice', 'conn-primary', 'primary')).attach_direct_foreground(
        barrier, direct, 'host-alice', None, None, account_only=True)
    if selection is not None: await bridge._bind_direct_provider_policy(attachment, selection)
    await bridge._bind_direct_provider_policy_intent(attachment, intent)
    await bridge._bind_direct_foreground_root(attachment, owner='alice', host_session='chat', native_session='session', root='root')
    turn = _TurnState(); turn.root_operation_id = 'root'; turn.owner_account_id = 'host-alice'
    turn.foreground_attachment = attachment; turn.main_option_candidate = candidate
    await bridge._install_provider_root('session', turn)
    e.turn, e.direct = turn, direct
    actor = {'sessionID': 'session', 'actorID': 'main', 'executionRevision': 0, 'status': 'idle', 'mode': 'main', 'parentActorID': None,
             'effectiveModel': {'providerID': 'conn-primary', 'modelID': 'primary'}, 'modelRequestOptions': snap}
    async def tree(session): return {'rootSessionID': 'session', 'sessions': {'session': {'actors': [actor]}}}
    bridge._read_managed_actor_tree = tree
    pre = await callbacks.dispatch(P + 'actor-route/preflight', {'sessionID': 'session', 'rootOperationID': 'root', 'actorID': 'main',
        'expectedExecutionRevision': 0, 'model': {'providerID': 'conn-primary', 'modelID': 'primary'}, 'modelSource': 'explicit'})
    actor.update(executionRevision=1, status='running', modelRequestOptions=pre['modelRequestOptions'])
    e.bound = await callbacks.dispatch(P + 'account/bind', {'rootOperationID': 'root', 'scopeKind': 'actor_execution', 'sessionID': 'session',
        'actorID': 'main', 'executionRevision': 1, 'modelRouteID': 'route-primary', 'connectionID': 'conn-primary', 'providerID': 'anthropic',
        'billingLane': 'metered_api', 'modelID': 'primary', 'bootstrap': pre['bootstrap'], 'modelRequestOptions': pre['modelRequestOptions']})
    e.identity = {'sessionID': 'session', 'rootOperationID': 'root', 'actorID': 'main', 'executionRevision': 1, 'innerTurnID': 'turn'}
    key = bridge._managed_turn_key(e.identity)
    entry = {'state': 'active', 'rootSessionID': 'session', 'owner': 'alice', 'ownerAdmissionBarrier': barrier, 'ownerAdmission': original,
             'ownerAdmissionAccount': 'host-alice', 'stableChatID': 'chat', 'holderID': 'holder',
             'effectiveModel': {'providerID': 'conn-primary', 'modelID': 'primary'}, 'mainOptionCandidate': candidate,
             'modelRequestOptions': pre['modelRequestOptions']}
    bridge._managed_actor_turns = {key: entry}; bridge._managed_actor_executions = {key[:4]: entry}
    return e


async def _teardown(e):
    await e.bridge._retire_provider_root('session', expected=e.turn)
    await e.barrier.release(e.direct); _current_admission.reset(e.token); await e.barrier.release(e.original); e.engine.dispose()


def _run(tag, policy_on, steps):
    async def main():
        e = await _setup(tag, policy_on=policy_on)
        try: await steps(e)
        finally: await _teardown(e)
    asyncio.run(main())


async def _refused(awaitable):
    try: await awaitable
    except Exception as error: return error
    raise AssertionError('expected a refusal')


async def _first_and_terminal(e, profile=PROFILE):
    b = e.bound
    first = await e.callbacks.dispatch(P + 'credential/lease', {'bindingID': b['bindingID'], 'expectedBindingRevision': b['bindingRevision'],
        'expectedCredentialRevision': b['credentialRevision'], 'crossModelAdmission': {'version': 1, 'turn': e.identity, 'expectedCoordinatorRevision': 1}})
    att = first['crossModelAttempt']; e.attempt = att
    request = {'bindingID': b['bindingID'], 'expectedRevision': b['bindingRevision'], 'crossModelTerminal': {'version': 1, 'turn': e.identity,
        'attemptID': att['attemptID'], 'attemptGeneration': att['attemptGeneration'], 'expectedCoordinatorRevision': att['coordinatorRevision'], 'profile': profile}}
    return first, request


def _issue(e, terminal):
    b = e.bound
    return e.callbacks.dispatch(P + 'credential/lease', {'bindingID': b['bindingID'], 'expectedBindingRevision': terminal['bindingRevision'],
        'expectedCredentialRevision': b['credentialRevision'], 'crossModelAlternative': {'version': 1, 'turn': e.identity,
        'expectedCoordinatorRevision': terminal['coordinatorRevision'], 'terminalAttemptID': terminal['attemptID']}})


def _control(e, method, key, issued):
    r = issued['attempt']
    return e.callbacks.dispatch(P + method, {'bindingID': r['primaryBindingID'], 'expectedRevision': r['primaryBindingRevision'], key: {
        'version': 1, 'turn': e.identity, 'attemptGeneration': 2, 'primaryBindingID': r['primaryBindingID'], 'primaryBindingRevision': r['primaryBindingRevision'],
        'expectedCoordinatorRevision': r['coordinatorRevision'], 'alternativeBindingID': r['alternativeBindingID'],
        'alternativeBindingRevision': r['alternativeBindingRevision'], 'attemptID': r['attemptID']}})


def _bindings(e):
    with e.engine.connect() as c:
        return c.exec_driver_sql('SELECT id,model_route_id,selected_account_id,state FROM provider_operation_bindings ORDER BY created_at,id').all()


def test_happy_chain_commits_the_alternative_once():
    async def steps(e):
        assert e.saved['route_profile_readiness'] == 'route_profile_ready' and e.saved['runtime_available'] is False
        assert e.bound['pendingCrossModel'] == {'version': 1, 'coordinatorRevision': 1}
        _first, terminal_request = await _first_and_terminal(e)
        assert e.attempt['attemptGeneration'] == 1 and e.attempt['coordinatorRevision'] == 2
        terminal = await e.callbacks.dispatch(P + 'account/attempt', terminal_request)
        assert terminal['state'] == 'auth_no_output_closed' and terminal['bindingRevision'] == e.bound['bindingRevision'] + 1
        issued = await _issue(e, terminal)
        assert issued['binding']['accountID'] == 'acct-alt' and issued['route']['route_id'] == 'route-alt', issued
        assert issued['attempt']['attemptGeneration'] == 2 and issued['attempt']['coordinatorRevision'] == terminal['coordinatorRevision'] + 1
        assert (await _control(e, 'account/attempt', 'crossModelDispatch', issued))['state'] == 'ready'
        committed = await _control(e, 'account/commit', 'crossModelAlternativeCommit', issued)
        assert committed['state'] == 'committed' and committed['alternativeBindingID'] == issued['attempt']['alternativeBindingID']
        rows = _bindings(e)
        assert len(rows) == 2 and [(r[1], r[2], r[3]) for r in rows if r[0] == committed['alternativeBindingID']] == [('route-alt', 'acct-alt', 'committed')]
        assert [r[3] for r in rows if r[0] == e.bound['bindingID']] == ['uncommitted']
        # A confirmed alternative commit permanently stops further advancement.
        await _refused(_issue(e, terminal))
        await _refused(_control(e, 'account/commit', 'crossModelAlternativeCommit', issued))
        assert len(_bindings(e)) == 2
    _run('happy', True, steps)


def test_non_401_outcome_issues_no_alternative():
    async def steps(e):
        b = e.bound
        _first, terminal_request = await _first_and_terminal(e, profile='anthropic.messages.429-rate-limit.v1')
        await _refused(e.callbacks.dispatch(P + 'account/attempt', terminal_request))  # pinned wire schema allows only the 401 profile
        att = e.attempt
        control = {'version': 1, 'turn': e.identity, 'attemptID': att['attemptID'], 'attemptGeneration': 1,
                   'expectedCoordinatorRevision': att['coordinatorRevision']}
        await e.callbacks.dispatch(P + 'account/attempt', {'bindingID': b['bindingID'], 'expectedRevision': b['bindingRevision'],
            'outcome': 'transient', 'accountID': 'acct-primary', 'crossModelControl': control})  # a real non-401 stop
        with e.engine.connect() as c:
            revision = c.exec_driver_sql('SELECT revision FROM provider_operation_bindings WHERE id=?', (b['bindingID'],)).scalar_one()
        late = dict(terminal_request['crossModelTerminal'], profile=PROFILE, expectedCoordinatorRevision=att['coordinatorRevision'] + 1)
        await _refused(e.callbacks.dispatch(P + 'account/attempt', {'bindingID': b['bindingID'], 'expectedRevision': revision, 'crossModelTerminal': late}))
        assert len(_bindings(e)) == 1
    _run('profile', True, steps)


def test_policy_off_binds_without_pending_cross_model():
    async def steps(e):
        assert e.saved is None and 'pendingCrossModel' not in e.bound
        await _refused(e.callbacks.dispatch(P + 'credential/lease', {'bindingID': e.bound['bindingID'], 'expectedBindingRevision': e.bound['bindingRevision'],
            'expectedCredentialRevision': e.bound['credentialRevision'], 'crossModelAdmission': {'version': 1, 'turn': e.identity, 'expectedCoordinatorRevision': 1}}))
        assert len(_bindings(e)) == 1
    _run('off', False, steps)


def test_alternative_account_made_ineligible_before_issue_commits_nothing():
    async def steps(e):
        _first, terminal_request = await _first_and_terminal(e)
        terminal = await e.callbacks.dispatch(P + 'account/attempt', terminal_request)
        e.store.set_entitlement(owner='alice', account_id='acct-alt', model_route_id='route-alt', eligible=False)
        await _refused(_issue(e, terminal))
        assert [r[1:] for r in _bindings(e)] == [('route-primary', 'acct-primary', 'uncommitted')]
    _run('ineligible', True, steps)


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn): fn(); n += 1
    print(n, 'ok')
