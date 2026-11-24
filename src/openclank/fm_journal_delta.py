"""Journal delta selection, staging and settlement (BQ-8, contract B/D/E; S08 journal synthesis).

Host side of the S08 journal selection. Nothing here dispatches inference, moves a cursor from a read, or uses a timer:

* ``stage``: one archive page (`ConversationArchive.read_journal_delta_page`) -> closed selection -> the existing S08
  ``s08_model_prepare`` phase (idempotent by the selection key). The S05 gates run later, at reservation, exactly as for every
  other S05/S08 job. Default Off (preference ``memory_policy_consolidation_enabled`` must be exactly True).
* ``settle``: the Native ``journal_delta_status`` read -> a typed :class:`JournalOutcomeProof` (minted only here, only from that
  read) -> ``ConversationArchive.ack_journal_delta``. Original outcome first, then the idempotent ACK; after a crash only the ACK
  is retried and inference is never rerun.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from src.openclank import fm_journal_coverage as core

PREF_KEY = 'memory_policy_consolidation_enabled'   # read as `is True`; unset = Off
WORK_KIND = 'synthesis'                            # per-work-kind model/thinking assignment (unset inherits, unanchored defers)
OUTPUT_KIND = 'journal_synthesis'
_PROOF_SEAL = object()


class JournalDeltaError(ValueError):
    """A closed, content-free refusal."""


@dataclass(frozen=True, eq=False)
class JournalOutcomeProof:
    """The recorded original outcome of one exact selection. Mint only with :func:`mint_outcome_proof`."""
    selection_sha256: str
    page_sha256: str
    prior_cursor: int
    end_seq: int
    outcome: str                    # 'proposal' | 'no_finding'
    native_receipt_ids: tuple
    rows: tuple                     # ((seq, revision, content_hash), ...) of the covered included rows
    _seal: object = None

    def __post_init__(self):
        if self._seal is not _PROOF_SEAL:
            raise ValueError('journal outcome proof can only be minted from the native status read')


# --- selection identity (contract B) ------------------------------------------------------------------------------------------

def selection_binding(page: dict, *, policy_fingerprint: str, authority_fingerprint: str) -> dict:
    return {'version': 1, 'kind': 'journal_delta_selection', 'account_id': page['account_id'], 'chat_id': page['chat_id'],
            'workspace_id': page['workspace_id'], 'consumer': core.CONSUMER, 'prior_cursor': page['ack_seq'],
            'high_seq': page['high_seq'], 'end_seq': page['end_seq'], 'page_sha256': page['page_sha256'],
            'policy_fingerprint': policy_fingerprint, 'authority_fingerprint': authority_fingerprint}


def selection_sha256(binding: dict) -> str:
    return core.sha256_hex(binding)


def idempotency_key(selection_digest: str) -> str:
    return 's08_' + selection_digest        # Native requires exactly this


def journal_object(page: dict, selection_digest: str) -> dict:
    """The Native ``selection.journal`` object: every page row (digest fields) plus source/authority for sourced rows only."""
    rows = []
    for row in page['rows']:
        item = core.digest_row(row)
        if row['class'] in (core.INCLUDED, core.ALREADY_COVERED):
            proof = row['proof']
            item['source'], item['authority'] = proof['source'], proof['authority']
        else:
            item['source'] = item['authority'] = None
        rows.append(item)
    return {'version': 1, 'chat_id': page['chat_id'], 'prior_cursor': page['ack_seq'], 'high_seq': page['high_seq'],
            'end_seq': page['end_seq'], 'page_sha256': page['page_sha256'], 'selection_sha256': selection_digest, 'rows': rows}


def journal_original(*, selected: dict, digest: str, route: dict, assignment_sha256: str, input_bound: int, limits, policy_refs: dict, cadence_policy=None) -> dict:
    """The one builder of the journal `S08ModelPrepareRequest`: used by the pass and by the S08 runner's prepare (no drift)."""
    original={'version': 1, 'selection': selected, 'mode': 'model', 'output_kind': OUTPUT_KIND, 'connection_predicate': None,
            'importance': {'state': 'skipped', 'reason': 'unset'}, 'snapshots_sha256': digest, 'assignment_sha256': assignment_sha256,
            'connection_id': route['connectionID'], 'model_route_id': route['modelRouteID'],
            'max_input_tokens': input_bound, 'max_output_tokens': limits.job_output_tokens,
            'daily_calls': limits.daily_calls, 'daily_tokens': limits.daily_tokens, 'policy_projection': policy_refs}
    if cadence_policy is not None: original['cadence_policy']=copy.deepcopy(cadence_policy)
    return original


def prepare_original(page: dict, *, owner: str, workspace_id: str, account_id: str, target_fingerprint: str, policy_fingerprint: str,
                     limits, route: dict, assignment_sha256: str, policy_refs: dict, cadence_policy=None) -> dict:
    """The exact `S08ModelPrepareRequest` (Wire (frozen), CHANGED 11-12b: the journal rides in ``selection``)."""
    selected, digest = JournalSelection(page).selected(owner=owner, workspace_id=workspace_id, account_id=account_id,
        target_fingerprint=target_fingerprint, policy_fingerprint=policy_fingerprint, limits=limits)
    return journal_original(selected=selected, digest=digest, route=route, assignment_sha256=assignment_sha256,
                            input_bound=page.get('input_bound', core.PAGE_BYTES), limits=limits, policy_refs=policy_refs, cadence_policy=cadence_policy)


def s05_gates_open(profile, *, owner, limits, input_bound) -> bool:
    """Read-only copy of the S05 `_reserve` idle/occupancy/budget tests (memory_job_admission `_reserve`): quiet window, no active foreground,
    no live reservation, daily calls/seconds/tokens. Fails closed. The authoritative check stays in Native/`_reserve` at reservation."""
    import time
    from src.openclank.memory_job_admission import WINDOW_MS, _conn
    try:
        profile.assert_current()
        now_tick, now_ms = profile.monotonic_tick(), time.time_ns() // 1_000_000
        with _conn(profile.path) as db:
            occ = db.execute('SELECT * FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
            if (not occ or not occ['verified'] or occ['coverage_generation'] != profile.coverage_generation
                    or occ['clock_generation'] != profile.clock_generation or now_tick < occ['last_quiet_tick']
                    or now_tick - occ['last_quiet_tick'] < limits.quiet_seconds * 1000):
                return False
            if (db.execute("SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown') LIMIT 1").fetchone()
                    or db.execute("SELECT 1 FROM fm_v2_s05_reservations WHERE state IN ('reserved','dispatched','uncertain') LIMIT 1").fetchone()):
                return False
            used = db.execute("SELECT COALESCE(sum(charged_calls),0),COALESCE(sum(charged_seconds),0),COALESCE(sum(charged_input_tokens+charged_output_tokens),0) "
                              "FROM fm_v2_s05_reservations WHERE owner_id=? AND COALESCE(settled_at_ms,reserved_at_ms)>? AND state!='revoked'",
                              (owner, now_ms - WINDOW_MS)).fetchone()
            return not (used[0] + 1 > limits.daily_calls or used[1] + limits.job_seconds > limits.daily_seconds
                        or used[2] + input_bound + limits.job_output_tokens > limits.daily_tokens)
    except Exception:
        return False


def journal_assignment_sha256(journal, route: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps([{'source': asdict(item.source), 'assignment': route} for item in journal._items], sort_keys=True,
                                     ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def decide(page: dict) -> str:
    """What the page allows: ``empty`` | ``blocked_overlap`` | ``stage`` (>=1 included row) | ``advance`` (nothing to synthesize, no gap)
    | ``blocked_gap`` (nothing to select and a gap keeps the prefix)."""
    rows = page['rows']
    if not rows:
        return 'empty'
    if any(row['class'] == core.GAP and row['reason'] == 'overlap_pending' for row in rows):
        return 'blocked_overlap'
    if any(row['class'] == core.INCLUDED for row in rows):
        return 'stage'
    return 'blocked_gap' if core.blocking_gaps(rows) else 'advance'


# --- outcome proof (frozen decision 7) ----------------------------------------------------------------------------------------

def mint_outcome_proof(*, status: Any, page: dict, selection_digest: str) -> JournalOutcomeProof:
    """Mint a proof from the Native ``journal_delta_status`` answer for exactly this selection; anything else refuses."""
    if (type(status) is not dict or status.get('version') != 1 or status.get('state') not in ('recorded_proposal', 'recorded_no_finding')
            or status.get('selection_sha256') != selection_digest or status.get('page_sha256') != page['page_sha256']
            or status.get('prior_cursor') != page['ack_seq'] or status.get('end_seq') != page['end_seq']
            or type(status.get('receipt_ids')) is not list or not status['receipt_ids']
            or any(type(item) is not str or not item for item in status['receipt_ids'])):
        raise JournalDeltaError('journal outcome is not a recorded outcome of this selection')
    rows = tuple((row['seq'], row['revision'], row['content_hash']) for row in page['rows'] if row['class'] == core.INCLUDED)
    return JournalOutcomeProof(selection_digest, page['page_sha256'], page['ack_seq'], page['end_seq'],
                               'proposal' if status['state'] == 'recorded_proposal' else 'no_finding',
                               tuple(status['receipt_ids']), rows, _PROOF_SEAL)


# --- resolvers handed to the archive (re-run identically at ACK time) ---------------------------------------------------------

def source_proof_for(*, source_authority, account_id: str, chat_id: str, origin_authority: Optional[Callable[[Any], Optional[dict]]] = None, view_only: bool = False) -> Callable[[dict], dict]:
    """``source_proof(row)``: the actual current ``host_user_body`` SourceRef proof (``CurrentHostUserSource.hydrate``) + origin authority."""
    from src.memery_external_sources import SourceRef

    def proof(row):
        ref = SourceRef(2 if row['workspace_id'] is None else 1, 'host_user_body', account_id, chat_id, row['actor_id'],
                        row['message_id'], row['part_id'], row['revision'], row['content_hash'], row['workspace_id'], row['project_id'])
        try:
            hydrated = source_authority.hydrate(ref)
        except Exception:
            return {'state': 'unavailable'}
        state = hydrated.get('state')
        if state != 'available' or type(hydrated.get('content')) is not str:
            return {'state': state if state in ('missing', 'denied') else 'unavailable'}
        # No origin authority source => fail closed for staging; a read-only count view needs none (it never reaches Native).
        authority = origin_authority(ref) if origin_authority is not None else ({} if view_only else None)
        if type(authority) is not dict:
            return {'state': 'unavailable'}
        return {'state': 'available', 'source': asdict(ref), 'authority': authority, 'size': len(hydrated['content'].encode('utf-8'))}
    return proof


def origin_authority_for(*, provider, target_authority, account_id: str, max_pages: int = 8) -> Callable[[Any], Optional[dict]]:
    """The exact S08/S06 origin projection reader (`provider._host_source_refs`, observation phase `o:`): the six authority fields of the
    `_HostObservationProjection` for the same SourceRef (the same fields `chat_source_bridge` puts in a phase-observation item), else None."""
    from src.memery_provider import _HostObservationProjection

    def authority(ref):
        after = 'o:'
        for _ in range(max_pages):
            rows, following = provider._host_source_refs(account_id=account_id, chat_id=ref.chat_id, message_id=ref.message_id,
                                                         target_authority=target_authority, limit=128, after_job_id=after)
            for row in rows:
                if type(row) is _HostObservationProjection and row.source == ref:
                    return {'origin_id': row.origin_id, 'origin_hash': row.origin_hash, 'captured_owner': row.captured_owner,
                            'current_fingerprint': row.current_fingerprint, 'rebind_id': row.rebind_id, 'rebind_hash': row.rebind_hash}
            if not following:
                return None
            after = following
        return None
    return authority


def dedupe_for(job_rows: Callable[[dict], list], journal_covered: Optional[Callable[[dict], Optional[dict]]] = None) -> Callable[[dict], Optional[dict]]:
    """``dedupe(row)``: overlap with other jobs on the same part/revision (contract B, H2).

    ``job_rows(row)`` returns the jobs of the SAME ``(message_id, part_id, revision)``. A pending/unknown job of ANY kind (completed-turn,
    paired, journal) blocks (`pending` -> `gap overlap_pending`); a succeeded/failed/cancelled completed-turn or paired job is a different
    purpose and leaves the row `included`. `already_covered` comes ONLY from ``journal_covered(row)``: a prior recorded JOURNAL outcome for the
    same part/revision, i.e. ``{'state': 'recorded_proposal'|'recorded_no_finding', 'receipt_id', 'job_id'}`` read through
    `journal_delta_status`. None (the default) never covers.
    """
    settled = ('succeeded', 'failed_terminal', 'cancelled')

    def verdict(row):
        for job in job_rows(row):
            if job['state'] not in settled:
                return {'state': 'pending'}
        covered = journal_covered(row) if journal_covered is not None else None
        if type(covered) is dict and covered.get('state') in ('recorded_proposal', 'recorded_no_finding') and covered.get('receipt_id') and covered.get('job_id'):
            return {'state': 'covered', 'receipt_id': covered['receipt_id'], 'job_id': covered['job_id']}
        return None
    return verdict


# --- one pass over one chat (chat guard held by the caller) -------------------------------------------------------------------

async def run_pass(*, archive, provider, lifecycle, assignment, account_id: str, chat_id: str, workspace_id: str, guard, authorize,
                   load_prefs: Callable[[], dict], source_proof, dedupe, policy_refs: Callable[[], dict],
                   limits, policy_fingerprint: str, before_issue: Callable[[], bool], hold: Optional[list] = None,
                   gates: Optional[Callable[[dict], Any]] = None, cadence_policy=None) -> dict:
    """Stage or settle at most one page. When `hold` is a list, the page of a staged/selected run is appended (for the runner). Returns a closed, content-free result dict; never raises for a refusal."""
    if load_prefs().get(PREF_KEY) is not True:
        return {'state': 'off'}
    owner, binding = lifecycle.binding.memory_owner, lifecycle.binding
    page = await asyncio.to_thread(lambda: archive.read_journal_delta_page(owner=account_id, chat_id=chat_id, workspace_id=workspace_id,
        authorize=authorize, source_proof=source_proof, dedupe=dedupe))
    what = decide(page)
    out = {'state': what, 'counts': core.counts(page['rows']), 'ack_seq': page['ack_seq'], 'high_seq': page['high_seq'], 'end_seq': page['end_seq']}
    if what in ('empty', 'blocked_overlap', 'blocked_gap'):
        return out

    def ack(coverage):
        return archive.ack_journal_delta(owner=account_id, chat_id=chat_id, workspace_id=workspace_id, authorize=authorize,
            lifecycle_owner=lifecycle, guard=guard, expected_cursor=page['ack_seq'], page_digest=page['page_sha256'], end_seq=page['end_seq'],
            coverage=coverage, source_proof=source_proof, dedupe=dedupe)

    async def try_ack(coverage):
        try:
            result = await asyncio.to_thread(ack, coverage)
        except ValueError as exc:
            return {**out, 'state': 'ack_refused', 'reason': str(exc)}
        return {**out, 'state': 'acked', 'replayed': result['replayed']}

    if what == 'advance':
        return await try_ack([])
    included = [row for row in page['rows'] if row['class'] == core.INCLUDED]
    try:
        route = assignment.resolve(WORK_KIND, _anchor(page, included, source_proof))
    except (ValueError, KeyError):
        return {**out, 'state': 'deferred', 'reason': 'route_unavailable'}   # unanchored/unset: defers, never guesses
    # Same input bound and assignment commitment as the S08 runner computes, so the original is byte-identical to the runner's prepare.
    journal = JournalSelection(page)
    try:
        _operation, page['input_bound'] = journal.model_input(lifecycle.source_authority.hydrate, limits.job_output_tokens)
    except JournalDeltaError:
        return {**out, 'state': 'deferred', 'reason': 'source_changed'}
    refs = policy_refs()
    original = prepare_original(page, owner=owner, workspace_id=workspace_id, account_id=account_id,
        target_fingerprint=binding.fingerprint, policy_fingerprint=policy_fingerprint, limits=limits, route=route,
        assignment_sha256=journal_assignment_sha256(journal, route), policy_refs=refs, cadence_policy=cadence_policy)
    digest = original['snapshots_sha256']
    status_request = {'version': 1, 'owner': owner, 'workspace_id': workspace_id, 'account_id': account_id,
                      'policy_projection': refs, 'selection_sha256': digest, 'prepare': original}
    status = await provider._host_journal_delta_status(status_request)
    state = status['state']
    if state in ('recorded_proposal', 'recorded_no_finding'):
        return await try_ack([mint_outcome_proof(status=status, page=page, selection_digest=digest)])  # only the ACK is (re)tried
    if state in ('absent', 'cancelled'):
        # H7: the S05 idle/occupancy/budget gates are read BEFORE any prepare; a closed gate is `deferred` and prepares nothing.
        if gates is not None and not await gates(original):
            return {**out, 'state': 'deferred', 'reason': 'gates_closed'}
        if hold is not None and state == 'absent':
            # The S08 runner prepares the identical original itself (one Native prepare, same S05 reservation): nothing is staged here.
            hold.append(page)
            return {**out, 'state': 'staged', 'job_id': None}
        try:
            job = await provider._host_journal_delta_prepare(original, before_issue=before_issue)
        except Exception as exc:
            if state == 'cancelled':
                # Native re-prepares the same key only after a verified pre-dispatch cancel; otherwise it is terminal: no retry loop.
                return {**out, 'state': 'not_coverage', 'native_state': state, 'reason': 'reprepare_refused:' + type(exc).__name__}
            raise
        if hold is not None: hold.append(page)
        return {**out, 'state': 'staged', 'job_id': job['job_id']}
    if state == 'selected' and hold is not None: hold.append(page)
    return {**out, 'state': {'selected': 'selected'}.get(state, 'not_coverage'), 'native_state': state}


def _anchor(page, included, source_proof):
    """The newest included row's SourceRef: the anchor of the per-kind assignment (unset inherits its initiating route)."""
    from src.memery_external_sources import SourceRef
    proof = included[-1]['proof']['source']
    return SourceRef(**proof)


async def run_chat_pass(*, profile, lifecycle, assignment, account_id: str, chat_id: str, run_job=None, staged: Optional[list] = None, automatic: bool = False) -> dict:
    """Compose the pass for one chat from the scheduler's objects: holds the chat guard, builds the archive resolvers."""
    from sqlalchemy import text
    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
    from src.openclank import fm_journal_coverage_reader as reader
    from src.openclank.memory_job_admission import _completed_turn_limits
    from src.openclank.memory_policy_projection import policy_coordinator
    source = lifecycle.source_authority
    binding = lifecycle.binding
    provider, workspace = lifecycle.provider, binding.memory_workspace

    def authorize(candidate, chat):
        if candidate != account_id:
            return False
        db = source._session_factory()
        try:
            row = db.execute(text('SELECT owner FROM sessions WHERE id=:chat'), {'chat': chat}).first()
            return bool(row and source._auth.account_id(row[0]) == account_id)
        finally:
            db.close()

    def load_prefs():
        with PREFERENCE_LOCK:
            return _load_for_user(lifecycle.owner)

    if load_prefs().get(PREF_KEY) is not True:
        return {'state': 'off'}
    # A store without the S08 model v2 profile cannot reserve a journal job: journal is unavailable there, quietly (nothing staged, no error, no log).
    readiness = getattr(provider, '_host_s08_policy_ready', None)
    if callable(readiness) and readiness('journal_synthesis') is not True:
        negotiate = getattr(provider, '_host_negotiate_s08_policy', None)
        if callable(negotiate):
            try:
                await negotiate(owner=binding.memory_owner, workspace_id=workspace)
            except Exception:
                pass
        if readiness('journal_synthesis') is not True:
            return {'state': 'unavailable'}
    base, limits = _completed_turn_limits(lifecycle.target_authority, assignment)
    coordinator = policy_coordinator()
    if coordinator is None:
        return {'state': 'unavailable'}
    refs = coordinator.checked_refs(lifecycle.owner, account_id, workspace)
    cadence_policy=None
    if automatic:
        from src.openclank.memory_policy_projection import exact_json, preference_inputs
        prefs=load_prefs(); interval=prefs.get('memory_policy_consolidation_interval_days')
        if interval is not None:
            if type(interval) is not int or not 1<=interval<=365: return {'state':'unavailable'}
            raw=exact_json({'preferences':preference_inputs(prefs),'can_manage_memory':True})
            if len(raw.encode('utf-8'))>65536 or hashlib.sha256(raw.encode('utf-8')).hexdigest()!=refs['account']['fingerprint']:
                return {'state':'unavailable'}
            cadence_policy={'version':1,'accepted_policy_input_json':raw}
    source_proof = source_proof_for(source_authority=source, account_id=account_id, chat_id=chat_id, origin_authority=origin_authority_for(provider=provider, target_authority=lifecycle.target_authority, account_id=account_id))
    dedupe = dedupe_for(reader.overlap_rows_for(profile.path, memory_owner=binding.memory_owner, account_id=account_id, chat_id=chat_id))
    async def gates(original):
        return await asyncio.to_thread(s05_gates_open, profile, owner=binding.memory_owner, limits=limits, input_bound=original['max_input_tokens'])

    async def one_pass(hold):
        async with lifecycle.mapping.async_chat_admission_lock(chat_id) as guard:
            def before_issue():
                try:
                    profile.assert_current()
                    lifecycle.mapping.validate_chat_guard(chat_id, guard)
                    return coordinator.checked_refs(lifecycle.owner, account_id, workspace) == refs
                except Exception:
                    return False
            return await run_pass(archive=source._archive, provider=provider, lifecycle=lifecycle, assignment=assignment, account_id=account_id,
                chat_id=chat_id, workspace_id=workspace, guard=guard, authorize=authorize, load_prefs=load_prefs, source_proof=source_proof,
                dedupe=dedupe, policy_refs=lambda: refs, limits=limits, policy_fingerprint=base['fingerprint'], before_issue=before_issue, hold=hold, gates=gates, cadence_policy=cadence_policy)

    return await settle(one_pass, run_job, staged, cadence_policy=cadence_policy, expected_refs=refs if cadence_policy is not None else None)


async def settle(one_pass, run_job, staged: Optional[list] = None, *, cadence_policy=None, expected_refs=None) -> dict:
    """Pass 1 (stage/read status) -> run the journal through the S08 runner -> pass 2 (status -> proof -> ACK)."""
    hold = []
    result = await one_pass(hold)
    if staged is not None and hold and result['state'] in ('staged', 'selected'):
        staged.append(JournalSelection(hold[0],cadence_policy=cadence_policy,expected_refs=expected_refs))   # handed to the scheduler's journal run step (H9); this wake never runs the model
    if result['state'] in ('staged', 'selected') and hold and run_job is not None:
        # The runner takes its own chat guards (the pass guard is released here). A refusal (busy, budget, route, source changed) is
        # `deferred`, never failed; nothing was written. A recorded outcome is then ACKed by a second pass: status -> proof -> ACK only,
        # so a crash after the commit retries only the ACK and never calls the model again.
        try:
            ran = await run_job(JournalSelection(hold[0],cadence_policy=cadence_policy,expected_refs=expected_refs))
        except Exception:
            ran = False
        if not ran:
            return {**result, 'state': 'deferred', 'reason': 'run_unavailable'}
        result = await one_pass(None)
    return result


# --- the original owner selection of one journal run (the S08 runner's journal variant, BQ-8 H5) --------------------------------

JOURNAL_INSTRUCTION = ('Review only the supplied host user messages of one chat, oldest first, each preceded by a line "--- message N of M ---". '
                       'Reply with exactly NO_FINDING when they contain nothing durable worth remembering. Otherwise reply with ONE short, '
                       'factual proposal (at most 4096 bytes) stating only what the messages say. Your reply is an untrusted proposal for human '
                       'review, never an approval.')
NO_FINDING = 'NO_FINDING'


@dataclass(frozen=True)
class JournalItem:
    """Stand-in for a consolidation evidence item in the shared guard/currentness code: only `.source` and `.event_id`."""
    source: Any
    event_id: str


class JournalSelection:
    """Immutable journal run input: the `included` rows of one page, in order. Created only from a classified page."""

    def __init__(self, page: dict, *, cadence_policy=None, expected_refs=None):
        self.cadence_policy=copy.deepcopy(cadence_policy)
        self.expected_policy_refs=copy.deepcopy(expected_refs)
        from src.memery_external_sources import SourceRef
        included = [row for row in page['rows'] if row['class'] == core.INCLUDED]
        if not included or any(row['actor_id'] != 'main' or row['role'] != 'user' for row in included):
            raise JournalDeltaError('journal selection needs included user rows')   # assistant bodies are omitted in this profile
        self.page = page
        self.rows = tuple(included)
        self._items = tuple(JournalItem(SourceRef(**row['proof']['source']), 's08journalrow_%d' % row['seq']) for row in included)

    @property
    def anchor(self):
        return self._items[-1].source

    def pairs(self):
        return tuple((item, None) for item in self._items)

    def selected(self, *, owner, workspace_id, account_id, target_fingerprint, policy_fingerprint, limits):
        """(`selection` object of the prepare request, selection digest)."""
        from src.memery_consolidation import s08_queue_limits
        digest = selection_sha256(selection_binding(self.page, policy_fingerprint=policy_fingerprint, authority_fingerprint=target_fingerprint))
        return ({'version': 1, 'owner': owner, 'workspace_id': workspace_id, 'project_id': None, 'source_account_id': account_id,
                 'idempotency_key': idempotency_key(digest), 'target_fingerprint': target_fingerprint, 'policy_fingerprint': policy_fingerprint,
                 **s08_queue_limits(limits), 'candidates': [], 'admissions': [],
                 'journal': journal_object(self.page, digest)}, digest)

    def model_input(self, hydrate, output_tokens: int) -> tuple:
        """(operation input, input token bound). Every body is hydrated now and must still be the exact row (hash) or the run aborts."""
        import hashlib
        parts = []
        for index, (row, item) in enumerate(zip(self.rows, self._items), 1):
            hydrated = hydrate(item.source)
            content = hydrated.get('content') if type(hydrated) is dict and hydrated.get('state') == 'available' else None
            if type(content) is not str or hashlib.sha256(content.encode('utf-8')).hexdigest() != row['content_hash']:
                raise JournalDeltaError('journal source changed')
            parts.append('--- message %d of %d ---\n%s' % (index, len(self.rows), content))
        body = '\n'.join(parts)
        bound = max(1, math.ceil(len(body.encode('utf-8')) / 2) + 1024)
        return ({'messages': [{'role': 'system', 'content': JOURNAL_INSTRUCTION}, {'role': 'user', 'content': body}],
                 'maxOutputTokens': output_tokens}, bound)

    def source_unchanged(self, hydrate) -> bool:
        try:
            self.model_input(hydrate, 1)
            return True
        except JournalDeltaError:
            return False


def validate_model_proposal(text) -> dict:
    """One proposal or the explicit `no_finding`; anything else is an invalid (failed) proposal, never a write."""
    if type(text) is not str:
        raise ValueError('journal proposal unavailable')
    value = text.strip()
    if value == NO_FINDING:
        return {'version': 1, 'kind': 'no_proposal'}
    if not value or len(value.encode('utf-8')) > 4096 or value != value.strip():
        raise ValueError('journal proposal unavailable')
    return {'version': 1, 'kind': 'journal_proposal', 'content': value}
