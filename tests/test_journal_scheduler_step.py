"""BQ-8 H9: the journal run is a sibling step after `_discover_one_s08_page` (FAKE callback/lifecycle; real TaskScheduler and aux-action path)."""
import asyncio
from types import SimpleNamespace

import pytest

from src.openclank import fm_journal_delta as delta
from src.task_scheduler import TaskScheduler
from tests.test_journal_runner import hydrate, page


def make(monkeypatch, *, pref=True, gate=True, staged=('c1',), accounts=('acct',), result=True):
    calls = SimpleNamespace(callback=[], gates=0)
    scheduler = TaskScheduler(session_manager=None)
    async def callback(state, lifecycle, selection, origins, router, *, output_kind, journal=None):
        calls.callback.append((lifecycle.owner, selection, origins, output_kind, journal))
        return result
    scheduler._s05_registry_state = SimpleNamespace(s08_rules_callback=callback)
    registry = {}
    for account in accounts:
        lifecycle = SimpleNamespace(owner='owner-' + account, binding=SimpleNamespace(memory_owner='mo'), target_authority=object(),
                                    source_authority=SimpleNamespace(hydrate=hydrate))
        registry[account] = (lifecycle, object(), object())
        scheduler._journal_staged[account] = {chat: delta.JournalSelection(page()) for chat in staged}
    limits = SimpleNamespace(job_output_tokens=512)
    monkeypatch.setattr('src.openclank.memory_job_admission._completed_turn_limits', lambda *_: ({}, limits))
    monkeypatch.setattr('routes.prefs_routes._load_for_user', lambda owner: {delta.PREF_KEY: True} if pref else {})
    def gates(profile, **kw):
        calls.gates += 1
        return gate
    monkeypatch.setattr(delta, 's05_gates_open', gates)
    profile = SimpleNamespace(assert_current=lambda: None)
    return scheduler, profile, registry, calls


def step(scheduler, profile, registry):
    asyncio.run(scheduler._run_one_journal_selection(profile, registry))


def test_gate_closed_defers_without_running_or_burning_anything(monkeypatch):
    scheduler, profile, registry, calls = make(monkeypatch, gate=False)
    step(scheduler, profile, registry)
    assert calls.gates == 1 and calls.callback == [] and list(scheduler._journal_staged['acct']) == ['c1'] and not scheduler._journal_settle


def test_gate_open_runs_one_job_and_queues_the_settle(monkeypatch):
    scheduler, profile, registry, calls = make(monkeypatch)
    step(scheduler, profile, registry)
    (owner, selection, origins, kind, journal), = calls.callback
    assert (owner, selection, origins, kind) == ('owner-acct', None, (), 'journal_synthesis') and type(journal) is delta.JournalSelection
    assert scheduler._journal_staged['acct'] == {} and scheduler._journal_settle == {'acct': {'c1'}}      # settled by the staging pass next wake


def test_a_refused_run_keeps_the_selection_staged(monkeypatch):
    scheduler, profile, registry, calls = make(monkeypatch, result=False)       # `_reserve` raised idle/budget unavailable inside the callback
    step(scheduler, profile, registry)
    assert len(calls.callback) == 1 and list(scheduler._journal_staged['acct']) == ['c1'] and not scheduler._journal_settle


def test_preference_off_is_inert(monkeypatch):
    scheduler, profile, registry, calls = make(monkeypatch, pref=False)
    step(scheduler, profile, registry)
    assert calls.gates == 0 and calls.callback == [] and list(scheduler._journal_staged['acct']) == ['c1']


def test_at_most_one_selection_per_wake_and_owners_rotate(monkeypatch):
    scheduler, profile, registry, calls = make(monkeypatch, staged=('c1', 'c2'), accounts=('a1', 'a2'))
    step(scheduler, profile, registry)
    assert len(calls.callback) == 1
    step(scheduler, profile, registry)
    assert [c[0] for c in calls.callback] == ['owner-a1', 'owner-a2']
    step(scheduler, profile, registry); step(scheduler, profile, registry)
    assert len(calls.callback) == 4


def test_the_existing_s08_step_runs_first_and_the_journal_step_is_a_sibling_after_it():
    scheduler = TaskScheduler(session_manager=None)
    order = []
    def rec(name):
        async def fn(*a, **kw):
            order.append(name)
        return fn
    for name in ('_stage_one_completed_turn_chat', '_reconcile_paired_publication_ticket', '_reconcile_source_publication_ticket',
                 '_stage_one_assistant_component_chat', '_reconcile_one_s08_original', '_reconcile_one_maintenance_source',
                 '_discover_one_s08_page', '_run_one_journal_selection'):
        setattr(scheduler, name, rec(name))
    scheduler._s05_pair_turn = True                      # flips to False: the S08 branch is reached
    scheduler._s05_controller = (SimpleNamespace(assert_current=lambda: None, path='/nonexistent/none.db'), {})
    try:
        asyncio.run(scheduler._select_completed_turn())
    except Exception:
        pass                                             # the completed-turn row scan after the steps needs a real store
    assert order.index('_discover_one_s08_page') + 1 == order.index('_run_one_journal_selection')
    assert order.index('_stage_one_completed_turn_chat') < order.index('_discover_one_s08_page')
