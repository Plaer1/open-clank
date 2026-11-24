"""Owner-scoped ordinary Usage presentation; no new persistence authority."""
from __future__ import annotations

from collections import defaultdict
from datetime import timezone
import os
from pathlib import Path
import sqlite3

from sqlalchemy import inspect

from services.stats.privacy import IdentityCatalog, identity_handle

IDENTITY_FILTERS = ('provider_id', 'account_id', 'actual_model', 'workspace_id',
                    'route_id', 'requested_model')


def canonical_labels(db, owner):
    """Read only label columns; missing catalogs never expose credential data."""
    cache = db.info.setdefault("usage_label_catalogs", {})
    if owner in cache:
        return cache[owner]
    from core.database import Session
    from core.provider_models import ProviderConnection, ProviderAccount, ProviderModelRoute
    labels = defaultdict(dict)
    from src.openclank.provider_family_catalog import bundled_provider_family_catalog
    for family in bundled_provider_family_catalog()['families']:
        labels['provider_id'][family['id']] = family['display_name']
    tables = set(inspect(db.get_bind()).get_table_names())
    for model, dimension, column in ((ProviderConnection, 'provider_id', ProviderConnection.label),
                                      (ProviderAccount, 'account_id', ProviderAccount.label),
                                      (ProviderModelRoute, 'route_id', ProviderModelRoute.display_name),
                                      (Session, 'session_id', Session.name)):
        if model.__tablename__ in tables:
            labels[dimension].update(db.query(model.id, column).filter(model.owner == owner).limit(100001).all())
    # Connection labels disambiguate connection-qualified observed models
    # without displaying internal connection IDs.
    for dimension in ('actual_model', 'requested_model'):
        labels[dimension].update({key: label for key, label in labels['provider_id'].items() if key.startswith('pcn_')})
    if ProviderModelRoute.__tablename__ in tables:
        for model, name in db.query(ProviderModelRoute.provider_model_id, ProviderModelRoute.display_name).filter(ProviderModelRoute.owner == owner).limit(100001).all():
            labels['actual_model'].setdefault(model, name)
            labels['requested_model'].setdefault(model, name)
    # File-policy workspaces use immutable account subjects. Read the canonical
    # username alias and label only, never workspace paths or permission payloads.
    from src.constants import APP_DB
    path = Path(os.environ.get('OPEN_CLANK_AUTHORITY_DB_PATH') or APP_DB).expanduser().absolute()
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1) as conn:
            conn.execute('PRAGMA query_only=ON')
            labels['workspace_id'].update(conn.execute(
                'SELECT w.id,w.name FROM file_policy_workspaces w '
                'JOIN file_policy_subject_aliases a ON a.subject_id=w.owner_subject_id '
                'WHERE a.username=? LIMIT 100001', (owner,)).fetchall())
    except sqlite3.Error:
        # A missing catalog means unavailable names, never path-derived labels.
        pass
    cache[owner] = dict(labels)
    return cache[owner]


def catalogs(owner, events, labels):
    return {field: IdentityCatalog(owner, field, [getattr(e, field, None) for e in events],
                                   labels=labels.get(field, {})) for field in IDENTITY_FILTERS}


def scope_for_filters(owner, filters):
    from services.stats.query import parse_scope
    def iso(value):
        if isinstance(value, (int, float)):
            from datetime import datetime
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
        return value
    return parse_scope(owner=owner, period=filters.get('period', 'all'),
                       timezone_name=filters.get('timezone', 'UTC'),
                       start=iso(filters.get('start')), end=iso(filters.get('end')))


def event_cohort(db, owner, filters, *, cancel_event=None, deadline=None):
    from services.stats.query import admitted_events
    scoped = {k: v for k, v in filters.items() if k in IDENTITY_FILTERS}
    scope = scope_for_filters(owner, filters)
    projection, truncated = admitted_events(db, scope, filters=scoped,
                                            cancel_event=cancel_event, deadline=deadline)
    ids = {e.session_id for e in projection.events if e.session_id} if scoped else None
    return scope, projection, truncated, ids


def session_metrics(db, owner, filters, *, session_ids=None, cancel_event=None, deadline=None):
    """Enrich the full bounded candidate set before sorting/paging."""
    from services.stats.query import _aggregate, _typed, _event_state
    from services.stats.pricing import price_event, resolve_profile
    from routes.stats_routes import _PRICING_PROFILES, _active_price_schedules
    scope, projection, truncated, selected = event_cohort(db, owner, filters,
                                                        cancel_event=cancel_event, deadline=deadline)
    labels = canonical_labels(db, owner)
    groups = defaultdict(list)
    for event in projection.events:
        if event.session_id and (session_ids is None or event.session_id in session_ids):
            groups[event.session_id].append(event)
    schedules = _active_price_schedules(db, cancel_event, deadline)
    result = {}
    for sid in set(session_ids or ()) | set(groups):
        from services.logging.projection import _check
        _check(cancel_event, deadline)
        events = groups[sid]
        metrics = {field: _typed(_aggregate([(getattr(e, field), _event_state(e, field)) for e in events])['value'],
                                    unit='tokens', state=_aggregate([(getattr(e, field), _event_state(e, field)) for e in events])['state'])
                   for field in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens')}
        # Input/output only: cache and reasoning may already be inclusive.
        values = [(getattr(e, field), _event_state(e, field)) for e in events for field in ('input_tokens', 'output_tokens')]
        total = _aggregate(values)
        components = [component for e in events for component in price_event(e, schedules,
                      inclusion_profile=resolve_profile(e, _PRICING_PROFILES), cancel_event=cancel_event, deadline=deadline)]
        priced = [c for c in components if c.amount is not None]
        currencies = {c.currency for c in priced}
        blocking = any(c.state == 'unpriced' and c.reason in {'missing_required_subset', 'invalid_count', 'no_exact_effective_schedule'} for c in components)
        amount = str(sum(c.amount for c in priced)) if priced and len(currencies) == 1 else None
        cost = {'value': amount, 'amount': amount, 'currency': next(iter(currencies)) if len(currencies) == 1 else None,
                'state': 'partial' if amount and blocking else 'estimated' if amount and any(c.state == 'estimated' for c in priced) else 'reported' if amount else 'unpriced'}
        identities = catalogs(owner, events, labels)
        workspace = identities['workspace_id'].choices()
        result[sid] = {'tokens': {**_typed(total['value'], unit='tokens', state=total['state']), 'supported': total['supported'], 'total': total['total']}, 'token_metrics': metrics,
                       'cost': cost, 'provider_identities': identities['provider_id'].choices(),
                       'model_identities': identities['actual_model'].choices(),
                       'workspace_identities': workspace,
                       'workspace_identity': workspace[0] if len(workspace) == 1 else None,
                       'coverage': {'state': 'partial_truncated' if truncated else projection.coverage,
                                    'source': 'admitted_stats_events'}}
    return result, scope, projection, truncated, selected, labels


def chart_events(db, events, metric, *, cancel_event=None, deadline=None):
    """Adapt admitted facts to existing bucket/group aggregators, read-only."""
    from types import SimpleNamespace
    from services.stats.query import StatsQueryError, TOKEN_VALUE_FIELDS, _aggregate, _event_state
    from services.stats.pricing import price_event, resolve_profile
    from routes.stats_routes import _PRICING_PROFILES, _active_price_schedules
    if metric not in TOKEN_VALUE_FIELDS | {'tokens', 'cost'}:
        raise StatsQueryError('unsupported chart metric')
    schedules = _active_price_schedules(db, cancel_event, deadline) if metric == 'cost' else None
    result, currencies = [], set()
    for event in events:
        if metric == 'tokens':
            aggregate = _aggregate([(event.input_tokens, event.input_tokens_state), (event.output_tokens, event.output_tokens_state)])
            value, state = aggregate['value'], aggregate['state']
        elif metric == 'cost':
            components = price_event(event, schedules, inclusion_profile=resolve_profile(event, _PRICING_PROFILES),
                                     cancel_event=cancel_event, deadline=deadline)
            priced = [c for c in components if c.amount is not None]
            found = {c.currency for c in priced}
            blocking = any(c.state == 'unpriced' and c.reason in {'missing_required_subset', 'invalid_count', 'no_exact_effective_schedule'} for c in components)
            currencies.update(found)
            value = sum(c.amount for c in priced) if priced and len(found) == 1 and not blocking else None
            state = 'estimated' if value is not None and any(c.state == 'estimated' for c in priced) else 'reported' if value is not None else 'unavailable'
        else:
            value, state = getattr(event, metric), _event_state(event, metric)
        attributes = {field: getattr(event, field, None) for field in ('event_time', *IDENTITY_FILTERS, 'actor_kind', 'source', 'status', 'observation_scope')}
        result.append(SimpleNamespace(**attributes, output_tokens=value, output_tokens_state=state))
    if metric == 'cost' and len(currencies) > 1:
        for event in result:
            event.output_tokens, event.output_tokens_state = None, 'unavailable'
    return result, next(iter(currencies)) if len(currencies) == 1 else None


def chart_values(rows, metric, currency):
    for row in rows:
        value = row['value']
        if metric == 'cost':
            value.update(unit='currency', currency=currency)
            if value['state'] in {'unavailable', 'unsupported'}: value['state'] = 'unpriced'
            if value.get('value') is not None: value['value'] = str(value['value'])
    return rows
