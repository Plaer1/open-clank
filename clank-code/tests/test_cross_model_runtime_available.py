"""runtime_available in the human cross-model policy projection.

Stubbed: the transaction (no real admission / converted store: a connection of
None stands for 'profile absent'), _require_route and _cross_model_policy.
Only the all-conditions-met case also stubs _human_cross_model_readiness; the
other cases run the real readiness function up to its early returns.
"""
from contextlib import contextmanager
from types import SimpleNamespace

from src.openclank.provider_store import ProviderStore


def _store(connection, policy, readiness=None):
    store = ProviderStore.__new__(ProviderStore)

    @contextmanager
    def tx(admission, *, write):
        yield object(), connection

    store._human_cross_model_transaction = tx
    store._cross_model_policy = lambda conn, route_id: policy
    store._require_route = lambda db, owner, route_id: SimpleNamespace(id=route_id, revision=3)
    if readiness is not None:
        store._human_cross_model_readiness = lambda *a: readiness
    return store


def _read(store):
    return store.human_cross_model_policy(SimpleNamespace(owner='o'), 'r1')


def _policy(enabled, n_alt):
    return {'version': 1, 'enabled': enabled, 'revision': 1, 'alternatives': [{'route_id': 'a%d' % i} for i in range(n_alt)]}


def test_no_profile_not_available():
    out = _read(_store(None, None))
    assert out['runtime_available'] is False and out['route_profile_readiness'] == 'profile_unavailable'


def test_policy_disabled_not_available():
    out = _read(_store(object(), _policy(False, 1)))
    assert out['runtime_available'] is False and out['route_profile_readiness'] == 'not_configured'


def test_alternative_not_eligible_not_available():
    out = _read(_store(object(), _policy(True, 2)))
    assert out['runtime_available'] is False and out['route_profile_readiness'] == 'route_unavailable'


def test_ready_profile_still_reports_runtime_unavailable():
    # Plan (provider-access/p08-b-host-coordinator.md, "Direct MAIN preflight continuity"):
    # runtime_available remains false because a settings read has no live per-turn driver witness.
    out = _read(_store(object(), _policy(True, 1), readiness='route_profile_ready'))
    assert out['runtime_available'] is False and out['route_profile_readiness'] == 'route_profile_ready'
