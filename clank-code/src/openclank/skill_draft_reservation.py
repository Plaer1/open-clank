"""Private, source-cold verifier for one Native skill-draft reservation.

The eventual skill-draft owner must install the qualified profile and retain the
session for its lifetime. This module does not prepare, reserve, issue or enable work.
"""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

_SEAL = object()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate reservation field')
        result[key] = value
    return result


def _assert_skill_draft_policy(session):
    """Recompute the existing accepted policy and teacher route, not a caller flag."""
    from src.openclank.memory_distill_work import automatic_skill_policy_snapshot
    original = session.attempt['reservation_original']['original']
    reservation = session.attempt['reservation_original']['reservation']
    snapshot = automatic_skill_policy_snapshot(lifecycle=session.lifecycle, assignment=session.assignment)
    for key in ('account_policy_input_json', 'policy_projection', 'policy_fingerprint',
                'target_fingerprint', 'assignment', 'assignment_sha256'):
        if snapshot[key] != original[key]:
            raise ValueError('skill draft policy changed')
    limits = snapshot['limits']
    if (original['connection_id'] != snapshot['assignment']['connection_id']
            or original['model_route_id'] != snapshot['assignment']['model_route_id']
            or original['daily_calls'] != limits.daily_calls or original['daily_tokens'] != limits.daily_tokens
            or original['max_output_tokens'] != min(limits.job_output_tokens, 16384)
            or reservation['quiet_seconds'] != limits.quiet_seconds
            or reservation['job_seconds'] != limits.job_seconds or reservation['daily_seconds'] != limits.daily_seconds
            or reservation['policy_fingerprint'] != snapshot['policy_fingerprint']
            or reservation['target_fingerprint'] != snapshot['target_fingerprint']):
        raise ValueError('skill draft policy changed')



class _SkillDraftReservation:
    """Bind a confirmed original to a single already-prepared utility handle."""
    def __init__(self, seal, *, session, prepared):
        if seal is not _SEAL:
            raise ValueError('skill draft reservation unavailable')
        self._seal, self.session, self.prepared = seal, session, prepared

    def _assert(self, prepared, router, utility, state):
        session = self.session
        provider = session.provider
        attempt = session.attempt
        reserve = attempt['reservation_original']
        original = reserve['original']
        reservation = reserve['reservation']
        if (self._seal is not _SEAL or getattr(session, '_seal', None) is not _SEAL
                or prepared is not self.prepared or prepared is not session.prepared
                or router is not session.router or session.handle is None
                or getattr(utility, 'owned_send', None) is not session.handle
                or getattr(provider, '_skill_draft_session', None) is not session
                or session.profile_witness is None
                or getattr(provider, '_skill_draft_profile_witness', None) is not session.profile_witness
                or not session.execution_owner.current()
                or utility.owner != session.lifecycle.owner or utility.account != original['account_id']
                or utility.auth is not session.lifecycle.target_authority._auth or not utility.account_current()
                or asyncio.get_running_loop().time() >= session.deadline):
            raise ValueError('skill draft reservation unavailable')
        utility.barrier.assert_live_utility(utility)
        profile = session.lifecycle.profile
        profile.assert_current()
        _assert_skill_draft_policy(session)
        preparation = provider._guarded_preparation
        if (preparation is None or preparation is not session.preparation or preparation.database_path != profile.path
                or (os.stat(profile.path).st_dev, os.stat(profile.path).st_ino) != (preparation.device, preparation.inode)):
            raise ValueError('skill draft database changed')
        expected_receipt = 'cs_skill_reserve_' + hashlib.sha256(original['operation_id'].encode('utf-8')).hexdigest()
        if (session.reserve_receipt_id != expected_receipt or session.reserve_request_sha256 != _sha(reserve)
                or attempt['job_id'] != original['operation_id']
                or attempt['attempt_id'] != 'skillattempt_' + hashlib.sha256(attempt['job_id'].encode()).hexdigest()
                or type(attempt['lease_epoch']) is not int or attempt['lease_epoch'] != 1
                or type(session.foreground_epoch) is not int or not re.fullmatch(r'[0-9a-f]{64}', session.reserve_request_sha256)):
            raise ValueError('skill draft reservation changed')
        expected = {'version': 1, 'original_sha256': _sha(original), 'request_sha256': session.reserve_request_sha256,
                    'account_id': original['account_id'], 'owner': original['owner'], 'job_id': attempt['job_id'],
                    'attempt_id': attempt['attempt_id'], 'lease_epoch': attempt['lease_epoch'],
                    'foreground_epoch': session.foreground_epoch}
        db = sqlite3.connect(Path(profile.path).as_uri() + '?mode=ro', uri=True, timeout=0.05, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            steps = [0]
            def progress():
                steps[0] += 1
                return steps[0] > 200
            db.set_progress_handler(progress, 1000)
            if (db.execute('PRAGMA schema_version').fetchone()[0] != preparation.schema_cookie
                    or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()[0] != preparation.database_id):
                raise ValueError('skill draft database changed')
            receipt = db.execute("""SELECT c.content_hash,c.operation_metadata_json,c.actor_type,c.actor_id,c.contract_version,
                c.source_event_id,o.payload_json,o.source_revision FROM fm_v2_change_sets c JOIN fm_v2_outbox o
                ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?
                WHERE c.owner_id=? AND c.change_set_id=? AND c.reason='skill_draft.reserve.v1'
                AND o.kind='skill_draft_reserve' AND length(CAST(c.operation_metadata_json AS BLOB))<=8192
                AND length(CAST(o.payload_json AS BLOB))<=8192""",
                ('outbox_' + expected_receipt + '_skill_draft_reserve', original['owner'], expected_receipt)).fetchone()
            if not receipt:
                raise ValueError('skill draft receipt unavailable')
            body = json.loads(receipt['operation_metadata_json'], object_pairs_hook=_unique)
            out = json.loads(receipt['payload_json'], object_pairs_hook=_unique)
            if (body != expected or out != expected or hashlib.sha256(receipt['operation_metadata_json'].encode()).hexdigest() != receipt['content_hash']
                    or (receipt['actor_type'], receipt['actor_id'], receipt['contract_version'], receipt['source_event_id'], receipt['source_revision'])
                    != ('automation', 'skill_draft', 'frankenmemory.v2', None, 0)):
                raise ValueError('skill draft receipt changed')
            if state == 'dispatched':
                dispatch_id = 'cs_skill_dispatch_' + hashlib.sha256(original['operation_id'].encode()).hexdigest()
                dispatch = db.execute("""SELECT c.content_hash,c.operation_metadata_json,c.actor_type,c.actor_id,c.contract_version,
                    c.source_event_id,o.payload_json,o.source_revision FROM fm_v2_change_sets c JOIN fm_v2_outbox o
                    ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=?
                    WHERE c.owner_id=? AND c.change_set_id=? AND c.reason='skill_draft.dispatch.v1'
                    AND o.kind='skill_draft_dispatch' AND length(CAST(c.operation_metadata_json AS BLOB))<=8192
                    AND length(CAST(o.payload_json AS BLOB))<=8192""",
                    ('outbox_' + dispatch_id + '_skill_draft_dispatch', original['owner'], dispatch_id)).fetchone()
                expected_dispatch = {'version': 1, 'state': 'dispatched', 'job_id': attempt['job_id'],
                    'attempt_id': attempt['attempt_id'], 'lease_epoch': attempt['lease_epoch'], 'request_sha256': _sha(attempt)}
                if (not dispatch or json.loads(dispatch['operation_metadata_json'], object_pairs_hook=_unique) != expected_dispatch
                        or json.loads(dispatch['payload_json'], object_pairs_hook=_unique) != expected_dispatch
                        or hashlib.sha256(dispatch['operation_metadata_json'].encode()).hexdigest() != dispatch['content_hash']
                        or (dispatch['actor_type'], dispatch['actor_id'], dispatch['contract_version'], dispatch['source_event_id'], dispatch['source_revision'])
                        != ('automation', 'skill_draft', 'frankenmemory.v2', None, 0)):
                    raise ValueError('skill draft dispatch receipt changed')
            manifest = db.execute("""SELECT selected_json,input_hash,content_hash FROM fm_v2_job_manifests
                WHERE owner_id=? AND job_id=? AND phase='selection'
                AND length(CAST(selected_json AS BLOB))<=65536 LIMIT 2""", (original['owner'], attempt['job_id'])).fetchall()
            if (len(manifest) != 1 or manifest[0]['selected_json'] != _canonical(original)
                    or manifest[0]['input_hash'] != expected['original_sha256']
                    or manifest[0]['content_hash'] != expected['original_sha256']):
                raise ValueError('skill draft original changed')
            row = db.execute("""SELECT r.*,a.state AS attempt_state,a.lease_owner AS attempt_lease_owner,
                j.state AS job_state,j.kind,j.current_attempt_id,j.input_hash,o.verified,
                o.foreground_epoch AS current_epoch,o.coverage_generation AS current_coverage,o.clock_generation AS current_clock
                FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.job_id=r.job_id
                AND a.attempt_id=r.attempt_id AND a.lease_epoch=r.lease_epoch JOIN fm_v2_jobs j ON j.owner_id=r.owner_id
                AND j.job_id=r.job_id JOIN fm_v2_s05_occupancy o ON o.singleton=1
                WHERE r.owner_id=? AND r.job_id=? AND r.attempt_id=? AND r.lease_epoch=?""",
                (original['owner'], attempt['job_id'], attempt['attempt_id'], attempt['lease_epoch'])).fetchone()
            if (not row or row['state'] != state or row['attempt_state'] != ('leased' if state == 'reserved' else 'running')
                    or row['job_state'] != 'active' or row['kind'] != 'skill_draft' or row['current_attempt_id'] != attempt['attempt_id']
                    or row['input_hash'] != expected['original_sha256'] or row['verified'] != 1
                    or row['foreground_epoch'] != session.foreground_epoch or row['current_epoch'] != session.foreground_epoch
                    or row['coverage_generation'] != reservation['coverage_generation'] or row['current_coverage'] != profile.coverage_generation
                    or row['clock_generation'] != reservation['clock_generation'] or row['current_clock'] != profile.clock_generation
                    or row['connection_id'] != original['connection_id'] or row['reserved_calls'] != 1 or row['charged_calls'] != 1
                    or row['settled_at_ms'] is not None or row['charged_seconds'] != row['reserved_seconds']
                    or row['charged_input_tokens'] != row['reserved_input_tokens'] or row['charged_output_tokens'] != row['reserved_output_tokens']
                    or row['attempt_lease_owner'] != reservation['worker_id'] or row['reserved_seconds'] != reservation['job_seconds']
                    or row['reserved_input_tokens'] != original['max_input_tokens'] or row['reserved_output_tokens'] != original['max_output_tokens']
                    or not 0 <= time.time_ns() // 1_000_000 - row['reserved_at_ms'] < row['reserved_seconds'] * 1000
                    or db.execute("SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown') LIMIT 1").fetchone()):
                raise ValueError('skill draft reservation changed')
            db.rollback()
        finally:
            db.close()
        profile.assert_current()
        utility.barrier.assert_live_utility(utility)
        if not utility.account_current() or provider._skill_draft_profile_witness is not session.profile_witness:
            raise ValueError('skill draft authority changed')

    def assert_reserved(self, prepared, router, utility):
        self._assert(prepared, router, utility, 'reserved')

    def assert_dispatched(self, prepared, router, utility):
        self._assert(prepared, router, utility, 'dispatched')


def _skill_draft_messages_match(original, messages):
    from src.agent_tools.model_interaction_tools import _TEACHER_SYSTEM_PROMPT
    if (type(messages) is not list or len(messages) != 2
            or any(type(item) is not dict or set(item) != {'role', 'content'} for item in messages)
            or messages[0].get('role') != 'system' or messages[0].get('content') != _TEACHER_SYSTEM_PROMPT
            or messages[1].get('role') != 'user' or type(messages[1].get('content')) is not str):
        return False
    prompt = messages[1]['content']
    if len(prompt.encode('utf-8')) > 32768:
        return False
    bound = len(_TEACHER_SYSTEM_PROMPT.encode('utf-8')) + len(prompt.encode('utf-8')) + 256
    return original['max_input_tokens'] == bound and _sha(messages) == original['prompt_sha256']


def _assert_skill_draft_source_content(session, witness):
    """Reconstruct the exact bounded prompt from the held, current source set."""
    from src.agent_tools.model_interaction_tools import _TEACHER_SYSTEM_PROMPT
    from src.memery_external_sources import SourceRef
    from src.openclank.memory_distill_work import (
        _distill_context_excerpt, _distill_prompt, _validate_distill_sources, automatic_selection_identity,
    )
    lifecycle = session.lifecycle
    original = session.attempt['reservation_original']['original']
    occurrences = original['ordered_sources']
    if automatic_selection_identity(occurrences) != original['selection_sha256']:
        raise ValueError('skill draft selection changed')
    roots = tuple(SourceRef(**item['source']) for item in occurrences)
    if len(roots) != len(witness.sources):
        raise ValueError('skill draft source set changed')
    for root, (source, guard) in zip(roots, witness.sources):
        if root != source or session.guards.get(root.chat_id) is not guard:
            raise ValueError('skill draft source set changed')
        lifecycle.for_source(source=root, guard=guard)
    authority = lifecycle.source_authority
    archive = authority._archive
    rows = _validate_distill_sources(roots=roots, occurrences=occurrences, authority=authority,
                                    archive=archive, account=lifecycle.account_id)
    times = [part['wallEndMS'] for group in rows for part in group]
    now = int(time.time() * 1000)
    if not times or any(type(value) is not int or value > now or now - value > 30 * 86400000 for value in times):
        raise ValueError('skill draft source window changed')
    contexts = _distill_context_excerpt(roots=roots, rows=rows, authority=authority,
                                       archive=archive, account=lifecycle.account_id)
    messages = [{'role': 'system', 'content': _TEACHER_SYSTEM_PROMPT},
                {'role': 'user', 'content': _distill_prompt(contexts)}]
    if not _skill_draft_messages_match(original, messages):
        raise ValueError('skill draft source content changed')


class _SkillDraftSendPermit:
    """Recheck exact source and prepared frame immediately before dispatch."""
    def __init__(self, seal, *, session, prepared):
        if seal is not _SEAL:
            raise ValueError('skill draft send unavailable')
        self._seal, self.session, self.prepared = seal, session, prepared

    def assert_current(self, prepared):
        from dataclasses import asdict
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, _ReviewCurrentness
        from src.openclank.operation_router import PreparedManagedOperation, _thaw_json
        session = self.session
        provider, lifecycle = session.provider, session.lifecycle
        original = session.attempt['reservation_original']['original']
        witness = session.currentness
        if (self._seal is not _SEAL or getattr(session, '_seal', None) is not _SEAL
                or type(prepared) is not PreparedManagedOperation or prepared is not self.prepared
                or prepared is not session.prepared or type(lifecycle) is not CompletedTurnLifecycleOwner
                or lifecycle.provider is not provider or getattr(provider, '_skill_draft_session', None) is not session
                or session.profile_witness is None
                or getattr(provider, '_skill_draft_profile_witness', None) is not session.profile_witness
                or not session.execution_owner.current() or session.state != 'dispatched'
                or type(witness) is not _ReviewCurrentness or witness.owner is not lifecycle
                or witness.assignment is not session.assignment or type(session.guards) is not dict
                or any(session.guards.get(source.chat_id) is not guard for source, guard in witness.sources)):
            raise ValueError('skill draft send unavailable')
        if ([asdict(source) for source, _guard in witness.sources]
                != [item['source'] for item in original['ordered_sources']]):
            raise ValueError('skill draft source set changed')
        request = prepared.request
        routes = prepared.routes
        payload = _thaw_json(request.input)
        wire = _thaw_json(prepared.payload)
        if (prepared.owner != lifecycle.owner or prepared.operation != 'chat.complete' or request.owner != lifecycle.owner
                or request.operation != 'chat.complete' or request.purpose != 'utility'
                or request.root_operation_id != original['operation_id'] or request.idempotency_key != original['operation_id']
                or request.model_route_id != original['model_route_id'] or request.grant_id != original['assignment']['provider_grant_id']
                or len(routes) != 1 or routes[0].connection_id != original['connection_id']
                or routes[0].model_route_id != original['model_route_id'] or routes[0].grant_id != original['assignment']['provider_grant_id']
                or type(payload) is not dict or set(payload) != {'messages', 'maxOutputTokens'}
                or type(wire) is not dict or wire.get('input') != payload or wire.get('artifactInputs') != []
                or wire.get('options') != {} or _thaw_json(request.options) != {} or request.artifacts
                or wire.get('rootOperationID') != original['operation_id'] or wire.get('idempotencyKey') != original['operation_id']
                or wire.get('operation') != 'chat.complete' or wire.get('routes') != [routes[0].to_wire()]
                or payload['maxOutputTokens'] != original['max_output_tokens']
                or not _skill_draft_messages_match(original, payload['messages'])):
            raise ValueError('skill draft prepared frame changed')
        if not callable(session.assert_preframe):
            raise ValueError('skill draft source boundary unavailable')
        _assert_skill_draft_policy(session)
        session.assert_preframe()
        witness.assert_current()
        _assert_skill_draft_source_content(session, witness)
        witness.assert_current()
        _assert_skill_draft_policy(session)
        utility = session.handle._utility
        if utility is None or utility.auth is not lifecycle.target_authority._auth or not utility.account_current():
            raise ValueError('skill draft account changed')
        utility.barrier.assert_live_utility(utility)
        return None


class _SkillDraftSession:
    """One inert managed handle for a confirmed, qualified Native reservation."""
    def __init__(self, seal, *, execution_owner, lifecycle, assignment, router, prepared, attempt, receipt, deadline):
        if seal is not _SEAL:
            raise ValueError('skill draft session unavailable')
        self._seal = seal
        self.execution_owner = execution_owner
        self.lifecycle, self.assignment, self.router, self.prepared = lifecycle, assignment, router, prepared
        self.provider = lifecycle.provider
        self.attempt = json.loads(_canonical(attempt))
        self.reserve_receipt_id = receipt['reserve_receipt_id']
        self.reserve_request_sha256 = receipt['request_sha256']
        self.foreground_epoch = receipt['foreground_epoch']
        self.preparation = self.provider._guarded_preparation
        self.profile_witness = self.provider._skill_draft_profile_witness
        self.deadline = deadline
        self.handle = None
        self.state = 'prepared'
        self.currentness = self.guards = self.assert_preframe = None

    def bind_sources(self, held):
        from dataclasses import asdict
        from src.openclank.chat_source_bridge import _ReviewCurrentness
        original = self.attempt['reservation_original']['original']
        if (self._seal is not _SEAL or getattr(self.provider, '_skill_draft_session', None) is not self
                or type(held) is not dict or held.get('original') != original
                or type(held.get('currentness')) is not _ReviewCurrentness or type(held.get('guards')) is not dict
                or not callable(held.get('assert_preframe'))):
            raise ValueError('skill draft source boundary unavailable')
        witness = held['currentness']
        if (witness.owner is not self.lifecycle or witness.assignment is not self.assignment
                or [asdict(source) for source, _ in witness.sources] != [item['source'] for item in original['ordered_sources']]
                or any(held['guards'].get(source.chat_id) is not guard for source, guard in witness.sources)):
            raise ValueError('skill draft source set changed')
        held['assert_preframe']()
        witness.assert_current()
        self.currentness, self.guards, self.assert_preframe = witness, held['guards'], held['assert_preframe']

    def clear_sources(self):
        self.currentness = self.guards = self.assert_preframe = None


def _new_reserved_skill_draft_session(*, execution_owner, lifecycle, assignment, router, prepared, held, reserve, receipt, deadline):
    """Create one inert owned handle only after an exact Native reserve response.

    This is source-cold: the qualified profile readiness method and owning
    orchestrator must exist before it can run. The caller must reconcile any
    ambiguous original and must never use an issued outcome to create a handle.
    """
    import math
    from src.openclank.skill_draft_run import _SkillDraftOwner
    from src.memery_provider import MemeryProvider
    from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner, _ReviewCurrentness
    from src.openclank.memory_job_admission import AssignmentAuthority
    from src.openclank.operation_router import ManagedOperationRouter, PreparedManagedOperation
    if (type(execution_owner) is not _SkillDraftOwner or type(lifecycle) is not CompletedTurnLifecycleOwner or type(assignment) is not AssignmentAuthority
            or type(router) is not ManagedOperationRouter or type(prepared) is not PreparedManagedOperation
            or type(held) is not dict or type(reserve) is not dict or set(reserve) != {'version', 'original', 'reservation'}
            or type(receipt) is not dict or type(deadline) is not float or not math.isfinite(deadline)
            or deadline <= asyncio.get_running_loop().time()):
        raise ValueError('skill draft session unavailable')
    provider = lifecycle.provider
    if (not isinstance(provider, MemeryProvider) or assignment.owner != lifecycle.owner
            or getattr(provider, '_skill_draft_session', None) is not None
            or not execution_owner.current()
            or execution_owner.lifecycle is not lifecycle or execution_owner.assignment is not assignment
            or execution_owner.router is not router
            or getattr(provider, '_skill_draft_profile_witness', None) is None
            or provider._guarded_preparation is None or lifecycle.profile.provider is not provider
            or held.get('original') != reserve.get('original') or type(held.get('currentness')) is not _ReviewCurrentness
            or held['currentness'].owner is not lifecycle or held['currentness'].assignment is not assignment
            or not callable(held.get('assert_preframe'))):
        raise ValueError('skill draft session unavailable')
    original, reservation = reserve['original'], reserve['reservation']
    if (type(original) is not dict or type(reservation) is not dict
            or type(reserve['version']) is not int or reserve['version'] != 1):
        raise ValueError('skill draft reservation unavailable')
    job = original.get('operation_id')
    if (type(job) is not str or not re.fullmatch(r'skilldraft_[0-9a-f]{64}', job)
            or original.get('account_id') != lifecycle.account_id or original.get('owner') != lifecycle.binding.memory_owner
            or original.get('workspace_id') != lifecycle.binding.memory_workspace
            or reservation.get('owner') != original['owner'] or reservation.get('job_id') != job
            or reservation.get('coverage_generation') != lifecycle.profile.coverage_generation
            or reservation.get('clock_generation') != lifecycle.profile.clock_generation
            or reservation.get('target_fingerprint') != lifecycle.binding.fingerprint):
        raise ValueError('skill draft reservation changed')
    attempt_id = 'skillattempt_' + hashlib.sha256(job.encode()).hexdigest()
    receipt_id = 'cs_skill_reserve_' + hashlib.sha256(job.encode()).hexdigest()
    expected = {'version': 1, 'state': 'reserved', 'original_sha256': _sha(original), 'request_sha256': _sha(reserve),
        'account_id': lifecycle.account_id, 'owner': original['owner'], 'job_id': job, 'attempt_id': attempt_id,
        'lease_epoch': 1, 'foreground_epoch': receipt.get('foreground_epoch'), 'reserve_receipt_id': receipt_id}
    if (receipt != expected or type(receipt.get('version')) is not int or type(receipt.get('lease_epoch')) is not int
            or type(receipt.get('foreground_epoch')) is not int or receipt['foreground_epoch'] < 0):
        raise ValueError('skill draft reservation outcome unavailable')
    lifecycle.profile.assert_current()
    if (lifecycle.target_authority.resolve(lifecycle.account_id) != lifecycle.binding
            or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)):
        raise ValueError('skill draft owner changed')
    held['assert_preframe']()
    attempt = {'version': 1, 'reservation_original': reserve, 'job_id': job, 'attempt_id': attempt_id, 'lease_epoch': 1}
    session = _SkillDraftSession(_SEAL, execution_owner=execution_owner, lifecycle=lifecycle, assignment=assignment, router=router,
        prepared=prepared, attempt=attempt, receipt=receipt, deadline=deadline)
    provider._skill_draft_session = session
    try:
        session.bind_sources(held)
        session.clear_sources()
        background = _SkillDraftReservation(_SEAL, session=session, prepared=prepared)
        session.handle = router.new_prepared_owned(prepared, background_reservation=background)
        return session
    except BaseException:
        if getattr(provider, '_skill_draft_session', None) is session:
            provider._skill_draft_session = None
        raise


def _current_skill_draft_session(session, *, for_status=False):
    if (type(session) is not _SkillDraftSession or session._seal is not _SEAL
            or getattr(session.provider, '_skill_draft_session', None) is not session
            or session.profile_witness is None
            or getattr(session.provider, '_skill_draft_profile_witness', None) is not session.profile_witness
            or not session.execution_owner.current()
            or session.provider._guarded_preparation is not session.preparation
            or session.lifecycle.profile.provider is not session.provider
            or session.lifecycle.target_authority.resolve(session.lifecycle.account_id) != session.lifecycle.binding
            or session.lifecycle.account_lifecycle.owner_has_active_operation(session.lifecycle.owner)
            or (not for_status and time.monotonic() >= session.deadline)):
        raise ValueError('skill draft session changed')
    session.lifecycle.profile.assert_current()
    if not for_status:
        _assert_skill_draft_policy(session)


async def _ready_reserved_skill_draft_session(session):
    """Wait for the one inert utility without holding source locks or sending a frame."""
    _current_skill_draft_session(session)
    if session.state != 'prepared' or session.handle is None or session.currentness is not None:
        raise ValueError('skill draft ready unavailable')
    session.state = 'ready_wait'
    try:
        result = await session.handle.ready(deadline=session.deadline)
    except BaseException:
        session.state = 'ready_unknown'
        raise
    if type(result) is not dict or set(result) != {'state'} or result['state'] not in ('ready', 'suppressed', 'unknown'):
        session.state = 'ready_unknown'
        raise ValueError('skill draft ready outcome unavailable')
    session.state = 'ready' if result['state'] == 'ready' else 'ready_' + result['state']
    return result


async def _dispatch_reserved_skill_draft_session(session, held):
    """Dispatch one exact Native original, then arm its existing owned handle once."""
    _current_skill_draft_session(session)
    if session.state != 'ready' or session.handle is None or session.currentness is not None:
        raise ValueError('skill draft dispatch unavailable')
    session.bind_sources(held)
    witness = session.currentness
    def current():
        try:
            _current_skill_draft_session(session)
            if session.currentness is not witness:
                return False
            session.assert_preframe()
            witness.assert_current()
            return True
        except Exception:
            return False
    try:
        if current() is not True:
            raise ValueError('skill draft source changed')
        original = json.loads(_canonical(session.attempt))
        session.state = 'dispatch_issued'
        session.dispatch_ticket = {}
        result = await session.provider._host_candidate_request(
            session.lifecycle.binding.memory_owner, session.lifecycle.binding.memory_workspace,
            {'phase': 'skill_draft_dispatch', 'request': original}, require_live=True,
            write_deadline=float(min(session.deadline, asyncio.get_running_loop().time() + 30.0)),
            before_issue=current, write_status=session.dispatch_ticket)
        expected = {'version': 1, 'state': 'dispatched', 'job_id': original['job_id'],
            'attempt_id': original['attempt_id'], 'lease_epoch': original['lease_epoch'], 'request_sha256': _sha(original)}
        if (type(result) is not dict or result != expected or type(result.get('version')) is not int
                or type(result.get('lease_epoch')) is not int or current() is not True):
            raise ValueError('skill draft dispatch outcome unavailable')
        session.state = 'dispatched'
        permit = _SkillDraftSendPermit(_SEAL, session=session, prepared=session.prepared)
        outcome = await session.handle.arm(before_issue=lambda: permit.assert_current(session.prepared), deadline=session.deadline)
        if type(outcome) is not dict or set(outcome) != {'state'} or outcome['state'] not in ('issued', 'suppressed', 'unknown'):
            raise ValueError('skill draft frame outcome unavailable')
        session.state = 'model_' + outcome['state']
        return outcome
    finally:
        session.clear_sources()


async def _wait_issued_skill_draft_session(session):
    """Observe the one issued handle; timeout and cancellation never reissue it."""
    from src.openclank.operation_router import ManagedOperationResult
    if (type(session) is not _SkillDraftSession or session._seal is not _SEAL
            or getattr(session.provider, '_skill_draft_session', None) is not session
            or session.state != 'model_issued' or session.handle is None or session.currentness is not None):
        raise ValueError('skill draft result unavailable')
    remaining = session.deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        session.state = 'model_unknown'
        return {'state': 'unknown'}
    session.state = 'model_wait'
    try:
        result = await asyncio.wait_for(session.handle.wait(), timeout=remaining)
    except asyncio.TimeoutError:
        session.state = 'model_unknown'
        return {'state': 'unknown'}
    except BaseException:
        session.state = 'model_unknown'
        raise
    original = session.attempt['reservation_original']['original']
    if (type(result) is not ManagedOperationResult or result.root_operation_id != original['operation_id']
            or result.operation != 'chat.complete' or result.model_route_id != original['model_route_id']
            or result.connection_id != original['connection_id']):
        session.state = 'model_unknown'
        raise ValueError('skill draft result changed')
    session.result = result
    session.state = 'model_complete'
    return {'state': 'observed', 'result': result}


def _assert_skill_draft_result_current(session, *, states):
    """Check the actual observed result and currently held source set."""
    from src.openclank.chat_source_bridge import _ReviewCurrentness
    from src.openclank.operation_router import ManagedOperationResult
    for_status = states == ('save_unknown',)
    _current_skill_draft_session(session, for_status=for_status)
    result = getattr(session, 'result', None)
    witness = session.currentness
    if (session.state not in states or type(result) is not ManagedOperationResult
            or result.state != 'complete' or result.committed is not True
            or session.handle is None or session.handle._task is None or not session.handle._task.done()
            or session.handle._task.cancelled() or session.handle._task.exception() is not None
            or session.handle._task.result() is not result or type(result.output.get('text')) is not str or type(witness) is not _ReviewCurrentness
            or witness.owner is not session.lifecycle or witness.assignment is not session.assignment
            or type(session.guards) is not dict or not callable(session.assert_preframe)):
        raise ValueError('skill draft save unavailable')
    session.assert_preframe()
    witness.assert_current()
    _assert_skill_draft_source_content(session, witness)
    witness.assert_current()
    if not for_status:
        _assert_skill_draft_policy(session)


def _assert_skill_draft_save_current(session):
    _assert_skill_draft_result_current(session, states=('saving',))


def _assert_skill_draft_status_current(session):
    if (type(getattr(session, 'save_original', None)) is not dict
            or _sha(session.save_original) != getattr(session, 'save_original_sha256', None)):
        raise ValueError('skill draft original unavailable')
    _assert_skill_draft_result_current(session, states=('save_unknown',))


def _begin_skill_draft_status(session, held):
    """Rebind sources to read only the one retained uncertain save original."""
    if (type(session) is not _SkillDraftSession or session.state != 'save_unknown'
            or session.currentness is not None):
        raise ValueError('skill draft status unavailable')
    session.bind_sources(held)
    try:
        _assert_skill_draft_status_current(session)
    except BaseException:
        session.clear_sources()
        raise


def _begin_skill_draft_save(session, held):
    """Bind a fresh held source set; no History or filesystem write occurs here."""
    if (type(session) is not _SkillDraftSession or session.state != 'model_complete'
            or session.currentness is not None):
        raise ValueError('skill draft save unavailable')
    session.bind_sources(held)
    session.state = 'saving'
    try:
        _assert_skill_draft_save_current(session)
    except BaseException:
        session.state = 'model_complete'
        session.clear_sources()
        raise
