"""Read-only validation of a committed, private cross-model capture."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AlternativeAttribution:
    state: str
    model: str | None = None
    connection_id: str | None = None
    provider_id: str | None = None
    route_id: str | None = None
    thinking: tuple[tuple[str, str], ...] = ()


def unknown_alternative_attribution():
    return AlternativeAttribution('unknown')

_FIELDS = frozenset({'version','sessionID','rootOperationID','actorID','executionRevision','innerTurnID',
    'primaryBindingID','primaryBindingRevision','coordinatorRevision','alternativeBindingID',
    'alternativeBindingRevision','attemptID','attemptGeneration','routeID','connectionID',
    'providerID','modelID','billingLane','catalogRevision','options'})


def validate_alternative_execution(db, proof, *, owner, account, chat, session, root, holder, generation, nonce):
    """Return only safe historical model/options when the exact original v4 committed.

    This is not a dispatch grant and does not assert the credential remains live.
    """
    from core.provider_models import ProviderOperationBinding
    if type(proof) is not dict or set(proof) != _FIELDS or type(proof['version']) is not int or proof['version'] != 1:
        raise ValueError('Alternative proof unavailable')
    for key in ('sessionID','rootOperationID','innerTurnID','primaryBindingID','alternativeBindingID',
                'routeID','connectionID','providerID','modelID','billingLane'):
        if type(proof[key]) is not str or not proof[key] or len(proof[key].encode('utf-8')) > 256:
            raise ValueError('Alternative proof unavailable')
    for key in ('primaryBindingRevision','coordinatorRevision','alternativeBindingRevision','catalogRevision'):
        if type(proof[key]) is not int or not 0 < proof[key] <= 9007199254740991:
            raise ValueError('Alternative proof unavailable')
    if (type(proof['executionRevision']) is not int or not 0 <= proof['executionRevision'] <= 9007199254740991
            or not all(isinstance(value, str) and value for value in (owner, account, chat, session, root, holder, nonce))
            or type(generation) is not int or generation < 0
            or proof['actorID'] != 'main' or type(proof['attemptGeneration']) is not int or proof['attemptGeneration'] != 2
            or proof['sessionID'] != session or proof['rootOperationID'] != root
            or proof['providerID'] != 'anthropic' or proof['billingLane'] != 'metered_api'
            or type(proof['attemptID']) is not str or re.fullmatch(r'[0-9a-f]{64}', proof['attemptID']) is None
            or type(proof['options']) is not dict or set(proof['options']) - {'reasoning_effort'}):
        raise ValueError('Alternative proof unavailable')
    effort = proof['options'].get('reasoning_effort')
    if effort is not None and (type(effort) is not str or not effort or len(effort) > 32 or any(ord(c) < 32 for c in effort)):
        raise ValueError('Alternative proof unavailable')
    rows = db.query(ProviderOperationBinding).filter_by(owner=owner, root_operation_id=root).limit(3).all()
    if len(rows) != 2 or {row.id for row in rows} != {proof['primaryBindingID'], proof['alternativeBindingID']}:
        raise ValueError('Alternative proof unavailable')
    primary = next(row for row in rows if row.id == proof['primaryBindingID'])
    alternative = next(row for row in rows if row.id == proof['alternativeBindingID'])
    if (primary.revision != proof['primaryBindingRevision'] or primary.state != 'uncommitted'
            or alternative.revision != proof['alternativeBindingRevision'] or alternative.state != 'committed'
            or alternative.actor_scope_key != primary.actor_scope_key or alternative.credential_owner != owner
            or alternative.grant_id is not None or alternative.engine_session_id != session or alternative.actor_id != 'main'
            or alternative.execution_revision != proof['executionRevision']
            or primary.engine_session_id != session or primary.actor_id != 'main'
            or primary.execution_revision != proof['executionRevision']
            or any(getattr(alternative, attr) != proof[field] for attr, field in (
                ('model_route_id','routeID'),('connection_id','connectionID'),('provider_id','providerID'),
                ('model_id','modelID'),('billing_lane','billingLane')))):
        raise ValueError('Alternative proof unavailable')
    raw = db.connection().exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=? AND owner=?', (primary.id, owner)).scalar_one()
    if type(raw) is not str or len(raw.encode('utf-8')) > 65536: raise ValueError('Alternative proof unavailable')
    try: coordinator = json.loads(raw)
    except (ValueError, TypeError, UnicodeError) as exc: raise ValueError('Alternative proof unavailable') from exc
    if type(coordinator) is not dict or set(coordinator) != {'version','revision','state','original','primary','policy','alternatives','active_index','active_attempt','alternative_attempt'}:
        raise ValueError('Alternative proof unavailable')
    original, alternate, h2 = coordinator['original'], coordinator['alternative_attempt'], coordinator['active_attempt']
    if type(original) is not dict or type(alternate) is not dict or type(h2) is not dict:
        raise ValueError('Alternative proof unavailable')
    if (coordinator['version'] != 4 or coordinator['state'] != 'alternative_committed'
            or coordinator['revision'] != proof['coordinatorRevision'] or coordinator['active_index'] != 1
            or original.get('owner') != owner or original.get('account_id') != account or original.get('chat_id') != chat
            or original.get('session_id') != session or original.get('root_id') != root or original.get('actor_id') != 'main'
            or original.get('execution_revision') != proof['executionRevision'] or original.get('inner_turn_id') != proof['innerTurnID']
            or original.get('holder_id') != holder or original.get('generation') != generation or original.get('root_nonce') != nonce
            or alternate.get('state') != 'committed' or alternate.get('nonce') != proof['attemptID']
            or alternate.get('generation') != 2 or alternate.get('coordinator_revision') != proof['coordinatorRevision']
            or alternate.get('original') != original or (alternate.get('binding') or {}).get('id') != alternative.id
            or (alternate.get('binding') or {}).get('revision') != alternative.revision - 1
            or (h2.get('terminal') or {}).get('profile') != 'anthropic.messages.401-auth-no-output.v1'):
        raise ValueError('Alternative proof unavailable')
    route = alternate.get('route')
    option_snapshot = {'route_id':proof['routeID'], 'catalog_revision':proof['catalogRevision'], 'options':proof['options']}
    if (type(route) is not dict or route.get('route_id') != proof['routeID'] or route.get('connection_id') != proof['connectionID']
            or route.get('provider_id') != proof['providerID'] or route.get('model_id') != proof['modelID']
            or route.get('lane') != proof['billingLane'] or route.get('grant_id') is not None
            or route.get('options') != option_snapshot or (alternative.policy_snapshot or {}).get('alternative_options') != option_snapshot
            or coordinator.get('alternatives') != [route]):
        raise ValueError('Alternative proof unavailable')
    return AlternativeAttribution('verified_alternative', model=proof['modelID'], connection_id=proof['connectionID'],
            provider_id=proof['providerID'], route_id=proof['routeID'], thinking=tuple(sorted(proof['options'].items())))
