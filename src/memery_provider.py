"""Memery provider backed by a server-owned native worker."""

from __future__ import annotations

from src.memery_writer_fence import connect_writer

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
from contextlib import AsyncExitStack
from collections.abc import Mapping
from datetime import datetime
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.middleware import (
    INTERNAL_TOOL_HEADER,
    INTERNAL_TOOL_OWNER_HEADER,
    INTERNAL_TOOL_WORKSPACE_HEADER,
)
from src.memory_provider import (
    MemoryProvider,
    MemoryRequestRejectedError,
    MemoryRecord,
    MemoryScope,
    MemorySearchHit,
    MemoryTransportError,
)
from src.memory_scope import chat_workspace, memory_owner
from src.openclank.code_performance import (
    CodeStage,
    code_performance_span,
    code_request_service,
    mark_code_request_queued,
    measured_code_query,
)

logger = logging.getLogger(__name__)


class _OriginalResetHandoff:
    """Opaque same-process custody of one original guarded reset handoff."""
    def __init__(self, provider, claim, frozen, preparation, transport, generation, process, owner_task, may_issue, policy, rag, barrier, witness):
        self.provider=provider; self.claim=claim; self.frozen=frozen; self.old_preparation=preparation
        self.transport=transport; self.generation=generation; self.process=process; self.owner_task=owner_task
        self.may_issue=may_issue; self.issue_reserved=False; self.issue_future=None; self.issue_ticket=None; self.issue_drained=None; self.receipt=None; self.new_preparation=None
        self.reset_witness=provider._reset_retirement_witness
        self.task=None; self.policy=policy; self.rag=rag; self.barrier=barrier; self.witness=witness; self.cleanup_proved=False
        self.policy_rebound=False; self.rag_rebound=False; self.policy_finished=False; self.rag_finished=False
        self.epoch=None; self.unsent_fact=None
        self.original_retired=False; self.attempt=None


_ORIGINAL_RESET_UNSENT_SEAL = object()

class _OriginalResetUnsent:
    """Opaque provider-owned fact for the exact retired original issue."""
    def __init__(self, seal, provider, token):
        if seal is not _ORIGINAL_RESET_UNSENT_SEAL:
            raise ValueError('invalid reset fact')
        self._seal=seal; self.provider=provider; self.token=token


_S08_POLICY_SESSION_SEAL = object()


class _S08PolicySession:
    """Provider-owned original attempt; never reconstructed from model arguments."""
    def __init__(self, seal, provider, lifecycle, selection, origins, projection, assignment, router, output_kind):
        if seal is not _S08_POLICY_SESSION_SEAL: raise ValueError('S08 session unavailable')
        self._seal=seal; self.provider=provider; self.lifecycle=lifecycle; self.selection=selection
        self.origins=origins; self.projection=projection; self.assignment=assignment; self.router=router
        self.output_kind=output_kind; self.state='new'; self.original=None; self.attempt=None
        self.prepared=None; self.handle=None; self.guards=None; self.policy_refs=None; self.base=None
        self.assignment_snapshot=None; self.snapshot_commitment=None; self.ticket=None; self.result=None; self.axis_flags=None
        self.profile_witness=provider._s08_policy_witness
        self.expected_policy_refs=None
        self.journal=None  # BQ-8: JournalSelection for output_kind 'journal_synthesis' (the journal variant of this runner)


class _S08BackgroundReservation:
    """Exact already-accounted S05 reservation, not a source or model grant."""
    def __init__(self, seal, session, prepared):
        if seal is not _S08_POLICY_SESSION_SEAL or type(session) is not _S08PolicySession:
            raise ValueError('S08 background reservation unavailable')
        self._seal=seal; self.session=session; self.prepared=prepared

    def _assert(self, prepared, router, utility, *, dispatched):
        import sqlite3
        import time
        from pathlib import Path
        session=self.session; provider=session.provider; lifecycle=session.lifecycle; attempt=session.attempt
        if asyncio.get_running_loop().time()>=session.deadline:
            raise ValueError('S08 reservation expired')
        if (self._seal is not _S08_POLICY_SESSION_SEAL or session._seal is not _S08_POLICY_SESSION_SEAL
            or getattr(provider,'_s08_policy_session',None) is not session or session.provider is not provider
            or prepared is not self.prepared or prepared is not session.prepared or router is not session.router
            or session.handle is None or getattr(utility,'owned_send',None) is not session.handle
            or getattr(utility,'owner',None)!=lifecycle.owner or getattr(utility,'account',None)!=lifecycle.account_id
            or not utility.account_current() or utility.auth is not lifecycle.target_authority._auth
            or type(attempt) is not dict or type(session.reserve_receipt_id) is not str
            or type(session.reserve_request_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}',session.reserve_request_sha256) is None
            or provider._s08_policy_witness is not session.profile_witness or not provider._host_s08_policy_ready(session.output_kind)):
            raise ValueError('S08 background reservation unavailable')
        utility.barrier.assert_live_utility(utility)
        prepared_db=provider._guarded_preparation; profile=lifecycle.profile
        if prepared_db is None or prepared_db is not session.profile_witness[4] or prepared_db.database_path!=profile.path:
            raise ValueError('S08 background database changed')
        st=os.stat(prepared_db.database_path,follow_symlinks=False)
        if (st.st_dev,st.st_ino)!=(prepared_db.device,prepared_db.inode): raise ValueError('S08 background database changed')
        db=sqlite3.connect(Path(prepared_db.database_path).as_uri()+'?mode=ro',uri=True,timeout=0.05,isolation_level=None)
        try:
            db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            # A bounded VM budget prevents a malformed index from holding the writer callback.
            budget=[0]
            def progress():
                budget[0]+=1
                return 1 if budget[0]>200 else 0
            db.set_progress_handler(progress,1000)
            if (db.execute('PRAGMA schema_version').fetchone()[0]!=prepared_db.schema_cookie
                or db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()[0]!=prepared_db.database_id):
                raise ValueError('S08 background database changed')
            receipt=db.execute("SELECT c.content_hash,c.operation_metadata_json,c.actor_type,c.actor_id,c.contract_version,c.source_event_id,o.payload_json,o.source_revision FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.owner_id=c.owner_id AND o.change_set_id=c.change_set_id AND o.event_id=? WHERE c.owner_id=? AND c.change_set_id=? AND c.reason='s08.model-reserve.v1' AND o.kind='s08_model_reserve' AND length(CAST(c.operation_metadata_json AS BLOB))<=8192 AND length(CAST(o.payload_json AS BLOB))<=8192",('outbox_'+session.reserve_receipt_id+'_s08_model_reserve',attempt['original']['selection']['owner'],session.reserve_receipt_id)).fetchone()
            def unique(pairs):
                out={}
                for key,value in pairs:
                    if key in out: raise ValueError('duplicate receipt field')
                    out[key]=value
                return out
            if not receipt: raise ValueError('S08 reservation receipt unavailable')
            body=json.loads(receipt['operation_metadata_json'],object_pairs_hook=unique); out=json.loads(receipt['payload_json'],object_pairs_hook=unique)
            expected={'version':'s08.model-reserve.v1','request_sha256':session.reserve_request_sha256,
                'owner':attempt['original']['selection']['owner'],'account_id':lifecycle.account_id,'job_id':attempt['job_id'],
                'attempt_id':attempt['attempt_id'],'lease_epoch':attempt['lease_epoch'],'foreground_epoch':session.foreground_epoch}
            if (body!=expected or out!=expected or hashlib.sha256(receipt['operation_metadata_json'].encode()).hexdigest()!=receipt['content_hash']
                or (receipt['actor_type'],receipt['actor_id'],receipt['contract_version'],receipt['source_event_id'],receipt['source_revision'])!=('automation','s08_model','frankenmemory.v2',None,0)):
                raise ValueError('S08 reservation receipt changed')
            row=db.execute("SELECT r.*,a.state AS attempt_state,a.lease_owner AS attempt_lease_owner,j.state AS job_state,j.kind,j.current_attempt_id,o.verified,o.foreground_epoch AS current_epoch,o.coverage_generation AS current_coverage,o.clock_generation AS current_clock FROM fm_v2_s05_reservations r JOIN fm_v2_attempts a ON a.owner_id=r.owner_id AND a.job_id=r.job_id AND a.attempt_id=r.attempt_id AND a.lease_epoch=r.lease_epoch JOIN fm_v2_jobs j ON j.owner_id=r.owner_id AND j.job_id=r.job_id JOIN fm_v2_s05_occupancy o ON o.singleton=1 WHERE r.owner_id=? AND r.job_id=? AND r.attempt_id=? AND r.lease_epoch=?",(expected['owner'],expected['job_id'],expected['attempt_id'],expected['lease_epoch'])).fetchone()
            original=attempt['original']; reservation=attempt['reservation_original']['reservation']
            if (not row or row['state']!=('dispatched' if dispatched else 'reserved') or row['attempt_state']!=('running' if dispatched else 'leased')
                or row['job_state']!='active' or row['kind']!='access_consolidation' or row['current_attempt_id']!=attempt['attempt_id']
                or row['verified']!=1 or row['foreground_epoch']!=session.foreground_epoch or row['current_epoch']!=session.foreground_epoch
                or row['coverage_generation']!=reservation['coverage_generation'] or row['current_coverage']!=reservation['coverage_generation']
                or row['clock_generation']!=reservation['clock_generation'] or row['current_clock']!=reservation['clock_generation']
                or row['connection_id']!=original['connection_id'] or row['reserved_calls']!=1 or row['charged_calls']!=1
                or row['attempt_lease_owner']!=reservation['worker_id']
                or row['reserved_input_tokens']!=original['max_input_tokens'] or row['reserved_output_tokens']!=original['max_output_tokens']
                or row['reserved_seconds']!=reservation['job_seconds']
                or not 0<=time.time_ns()//1_000_000-row['reserved_at_ms']<row['reserved_seconds']*1000
                or db.execute("SELECT 1 FROM fm_v2_s05_foreground WHERE state IN ('active','unknown') LIMIT 1").fetchone()):
                raise ValueError('S08 background reservation changed')
            db.rollback()
        finally:
            db.close()
        utility.barrier.assert_live_utility(utility)
        if not utility.account_current() or provider._s08_policy_witness is not session.profile_witness:
            raise ValueError('S08 background authority changed')
        return None

    def assert_reserved(self, prepared, router, utility):
        return self._assert(prepared,router,utility,dispatched=False)

    def assert_dispatched(self, prepared, router, utility):
        return self._assert(prepared,router,utility,dispatched=True)


class _S08ManagedSendPermit:
    """One exact managed frame; current source authority is rechecked at write."""
    def __init__(self, seal, session, prepared):
        if seal is not _S08_POLICY_SESSION_SEAL or type(session) is not _S08PolicySession:
            raise ValueError('S08 send permit unavailable')
        self._seal=seal; self.session=session; self.prepared=prepared

    def assert_current(self, prepared):
        from src.openclank.chat_source_bridge import _ReviewCurrentness
        session=self.session; provider=session.provider; lifecycle=session.lifecycle
        witness=getattr(session,'currentness',None)
        if (self._seal is not _S08_POLICY_SESSION_SEAL or session._seal is not _S08_POLICY_SESSION_SEAL
            or prepared is not self.prepared or prepared is not session.prepared
            or getattr(provider,'_s08_policy_session',None) is not session or session.state!='dispatched'
            or not provider._host_s08_policy_ready(session.output_kind) or type(session.attempt) is not dict
            or type(session.guards) is not dict or lifecycle.provider is not provider
            or type(witness) is not _ReviewCurrentness or witness.owner is not lifecycle or witness.assignment is not session.assignment
            or any(session.guards.get(source.chat_id) is not guard for source,guard in witness.sources)):
            raise ValueError('S08 send permit unavailable')
        witness.assert_current()
        utility=session.handle._utility
        if utility is None or utility.auth is not lifecycle.target_authority._auth or not utility.account_current():
            raise ValueError('S08 current account changed')
        utility.barrier.assert_live_utility(utility)
        return None


_SOURCE_TYPE_MAP = {
    "user": "human",
    "user_created": "human",
    "ai_agent": "ai",
    "agent_explicit": "ai",
    "auto_extracted": "auto_extracted",
}

# Internal only: this type is constructed at an authenticated host route, never
# decoded by the model-facing broker dispatcher.
@dataclass(frozen=True)
class _HostCandidateReview:
    owner: str
    workspace_id: str
    candidate_id: str
    candidate_digest: str
    expected_v2_head: int
    selected_paths: tuple[str, ...]
    request_key: str
    reason: str


_CANDIDATE_EMBEDDING_ADMISSION_SEAL = object()


@dataclass
class _HostCandidateEmbeddingAdmission:
    request: _HostCandidateReview
    source: object
    provider: object
    _seal: object
    used: bool = False


def _canonical_index_sql(sql):
    """Ignore SQL formatting outside quoted literals, never inside them."""
    if not isinstance(sql, str): return ''
    result = []
    quote = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            result.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1; result.append(sql[index])
                else:
                    quote = None
        elif char in ("'", '"', '`'):
            quote = char; result.append(char)
        elif not char.isspace():
            result.append(char.lower())
        index += 1
    return ''.join(result) if quote is None else ''


def _provider_source_ref_error(provider, source):
    """Validate a host source and require the current sealed account-chat profile for v2."""
    from src.memery_external_sources import _source_ref_error
    from src.memery_database import GuardedStorePreparation
    error = _source_ref_error(source)
    if error:
        return error
    if source.version == 2:
        prepared = getattr(provider, '_guarded_preparation', None)
        if type(prepared) is not GuardedStorePreparation:
            return 'account_chat_unavailable'
        try:
            prepared.assert_account_chat()
        except Exception:
            return 'account_chat_unavailable'
        if provider._guarded_preparation is not prepared:
            return 'account_chat_unavailable'
    return None


def _provider_source_mapping_error(provider, value):
    from src.memery_external_sources import SourceRef
    if type(value) is not dict:
        return 'invalid_source_ref'
    try:
        source = SourceRef(**value)
    except (TypeError, ValueError):
        return 'invalid_source_ref'
    return _provider_source_ref_error(provider, source)


def _validate_candidate_source_guard(provider, request, source, guard):
    from src.memery_external_sources import SourceRef, _source_ref_error
    from src.openclank.chat_source_bridge import _HostChatFenceCapability
    if (type(request) is not _HostCandidateReview or type(source) is not SourceRef or _provider_source_ref_error(provider, source)
        or type(guard) is not _HostChatFenceCapability or guard.provider is not provider
        or guard.account != source.account_id or guard.chat_id != source.chat_id
        or guard.binding.memory_owner != request.owner or guard.binding.memory_workspace != request.workspace_id):
        raise MemoryRequestRejectedError('candidate source authority unavailable')
    guard.validate(provider=provider,owner=guard.owner,chat_id=source.chat_id)


def _admit_host_candidate_embedding(provider, request, source, guard):
    """Mint one fixed embedding dispatch while the exact source guard is live."""
    _validate_candidate_source_guard(provider,request,source,guard)
    return _HostCandidateEmbeddingAdmission(request,source,provider,_CANDIDATE_EMBEDDING_ADMISSION_SEAL)


@dataclass(frozen=True)
class _HostCandidateRejection:
    owner: str
    workspace_id: str
    candidate_id: str
    candidate_digest: str
    association_sha256: str
    source_account_id: str
    source_chat_id: str
    request_key: str
    reason: str


@dataclass(frozen=True)
class _HostCandidatePreview:
    owner: str
    workspace_id: str
    candidate_id: str
    reason: str


@dataclass(frozen=True)
class _HostSourceProjection:
    owner: str
    workspace_id: str
    job_id: str
    source: object
    policy_fingerprint: str


@dataclass(frozen=True)
class _HostPairedSourceProjection:
    owner: str
    workspace_id: str
    job_id: str
    source: object
    policy_fingerprint: str
    transition_class: Optional[str] = None
    input_sha256: Optional[str] = None


# A raw candidate is private to the guarded async inventory join and grants nothing.
_HOST_ERASED_INVENTORY = object()

@dataclass(frozen=True)
class _HostPairedErasedCandidate:
    projection: _HostPairedSourceProjection

@dataclass(frozen=True)
class _HostPairedSourceCandidate:
    projection: _HostPairedSourceProjection
    attempt_id: str
    lease_epoch: int
    content: str


@dataclass(frozen=True)
class _HostPairedReview:
    owner: str
    workspace_id: str
    account_id: str
    candidate_id: str
    candidate_digest: str
    association_sha256: str
    request_key: str
    reason: str
    action: str


@dataclass(frozen=True)
class _HostOwnerTransition:
    database_id: str
    account_id: str
    operation_id: str
    step: str
    generation: int
    action: str
    owner: str
    new_owner: str
    expected_source_fingerprint: str
    expected_target_fingerprint: str
    prior_receipt_id: Optional[str] = None
    prior_receipt_hash: Optional[str] = None
    rag_relocation: Optional[Dict[str, str]] = None
    version: int = 1


@dataclass(frozen=True)
class _HostOwnerPurge:
    database_id: str
    account_id: str
    operation_id: str
    step: str
    generation: int
    action: str
    owner: str
    expected_source_fingerprint: str
    version: int = 1


@dataclass(frozen=True)
class _ExternalHistoryItem:
    kind: str
    event_id: str
    block_id: str
    revision: int
    receipt_sha256: str
    source_id: str
    origin_id: str
    polarity: Optional[str] = None

@dataclass(frozen=True)
class _ExternalHistoryClaim:
    version: int
    workspace_id: str
    attribution: str
    asserted_store_id: str
    import_id: str
    page: int
    items: tuple[_ExternalHistoryItem, ...]

def _validate_external_history_claim(claim: _ExternalHistoryClaim, workspace: str) -> None:
    import re
    opaque = lambda value,limit: type(value) is str and 0 < len(value) <= limit and re.fullmatch(r'[A-Za-z0-9_.:-]+',value) is not None
    digest = lambda value: type(value) is str and re.fullmatch(r'[0-9a-f]{64}',value) is not None
    if (type(claim) is not _ExternalHistoryClaim or type(claim.version) is not int or claim.version != 1
            or claim.workspace_id != workspace or claim.attribution != 'external_unverified'
            or not opaque(claim.asserted_store_id,128) or not digest(claim.import_id)
            or type(claim.page) is not int or not 0 <= claim.page <= 0xffffffff
            or type(claim.items) is not tuple or not 1 <= len(claim.items) <= 128):
        raise MemoryRequestRejectedError('external history unavailable')
    for item in claim.items:
        if type(item) is not _ExternalHistoryItem or item.kind not in ('found','admitted','context_admitted','helped'):
            raise MemoryRequestRejectedError('external history unavailable')
        if (item.kind=='helped' and item.polarity not in ('positive','negative')) or (item.kind!='helped' and item.polarity is not None):
            raise MemoryRequestRejectedError('external history unavailable')
        prefix = {'found':'cs_s06found_', 'admitted':'cs_s06admitted_', 'context_admitted':'cs_s06context_', 'helped':'cs_s06helped_'}[item.kind]
        if (type(item.event_id) is not str or not item.event_id.startswith(prefix) or not digest(item.event_id[len(prefix):])
                or not opaque(item.block_id,256) or type(item.revision) is not int or not 1 <= item.revision <= 0x7fffffffffffffff
                or not digest(item.receipt_sha256) or type(item.source_id) is not str or not item.source_id.startswith('hostpart_')
                or not digest(item.source_id[len('hostpart_'):]) or type(item.origin_id) is not str
                or not item.origin_id.startswith('cs_s06origin_') or not digest(item.origin_id[len('cs_s06origin_'):])):
            raise MemoryRequestRejectedError('external history unavailable')

@dataclass(frozen=True)
class _HostObservationProjection:
    owner: str
    workspace_id: str
    origin_id: str
    source: object
    captured_owner: str
    origin_hash: str
    current_fingerprint: str
    rebind_id: Optional[str] = None
    rebind_hash: Optional[str] = None


@dataclass(frozen=True)
class _HostAdmittedObservation:
    origin: _HostObservationProjection
    found_event_id: str
    tool_call_id: str
    attestation_id: str
    result_sha256: str
    dispatch_id: str
    body_sha256: str


@dataclass(frozen=True)
class _HostContextAdmittedObservation:
    origin: _HostObservationProjection
    found_event_id: str
    context_part_id: str
    attestation_id: str
    text_sha256: str
    dispatch_id: str
    body_sha256: str


@dataclass(frozen=True)
class _HostObservationRenew:
    owner: str
    workspace_id: str
    original_owner: str
    operation_id: str
    origin_id: str
    origin_hash: str
    captured_owner: str
    prior_receipt_id: Optional[str]
    prior_receipt_hash: Optional[str]
    previous_fingerprint: str
    current_fingerprint: str
    source: object
    rename_proof_id: str


@dataclass(frozen=True)
class _HostSourceCandidate:
    projection: _HostSourceProjection
    attempt_id: str
    lease_epoch: int
    content: str


@dataclass(frozen=True)
class _HostSourceRenew:
    owner: str
    workspace_id: str
    original_owner: str
    operation_id: str
    job_id: str
    source: object
    rename_proof_id: str


@dataclass(frozen=True)
class _HostMirrorPreview:
    owner: str
    workspace_id: str
    memory_id: str
    candidate_id: str
    origin_request_key: str
    prior_receipt_id: str
    expected_revision: int
    request_key: str
    reason: str
    selected_paths: tuple[str, ...]
    payload: Dict[str, Any]


@dataclass(frozen=True)
class _HostMirrorContext:
    owner: str
    workspace_id: str
    memory_id: str
    account_id: Optional[str] = None


@dataclass(frozen=True)
class _HostMirrorApply:
    owner: str
    workspace_id: str
    memory_id: str
    candidate_id: str
    origin_request_key: str
    prior_receipt_id: str
    expected_revision: int
    request_key: str
    reason: str
    selected_paths: tuple[str, ...]
    operation_json: str
    operation_sha256: str


@dataclass(frozen=True)
class _S09DigestLease:
    provider: Any
    manager: Any
    lifecycle: Any
    auth: Any
    account_lifecycle: Any
    barrier: Any
    admission: Any
    owner: str
    workspace: str
    account: str
    transport: Any
    generation: Any
    process: Any
    task: Any
    preparation: Any

    def current(self) -> bool:
        try:
            from core.models import get_session_manager_instance
            from src.openclank.account_request_barrier import AccountRequestBarrier, _Admission
            if (type(self.barrier) is not AccountRequestBarrier or type(self.admission) is not _Admission
                    or get_session_manager_instance() is not self.manager or self.manager.lifecycle_context is not self.lifecycle
                    or self.lifecycle.memory_provider is not self.provider or self.lifecycle.auth_manager is not self.auth
                    or self.lifecycle.account_request_barrier is not self.barrier
                    or self.lifecycle.account_owner_lifecycle is not self.account_lifecycle
                    or self.auth.account_id(self.owner) != self.account or self.auth.username_for_account_id(self.account) != self.owner
                    or self.account_lifecycle.owner_has_active_operation(self.owner)):
                return False
            self.barrier.assert_live_admission(self.owner,self.admission)
            scope=self.provider._scope(self.owner)
            return (scope.owner==self.owner and scope.workspace_id==self.workspace
                    and self.provider._owned_transport is self.transport and self.provider._owned_generation is self.generation
                    and self.provider._owner_task is self.task and not self.task.done()
                    and self.provider._guarded_preparation is self.preparation and self.transport.process is self.process
                    and self.process.returncode is None and self.transport.current(self.generation))
        except Exception:
            return False


_HOST_CANDIDATE_REVIEW = "_host_candidate_review"
# Operations Native advertises under native_memory_ops_v1; the list is compared for exact equality (order included)
# with NATIVE_MEMORY_OPS in native/memery/crates/memery-worker/src/main.rs (tests/test_native_ops_parity.py).
NATIVE_MEMORY_OPS = ("explicit_add_review_batch_v1", "curated_knowledge_snapshot_review_v1")
NATIVE_PREEXTRACTED_OPERATION = "preextracted_candidates_v1"
NATIVE_HOST_ANCHOR_OPERATION = "preextracted_host_anchor_v1"
NATIVE_UPDATE_BATCH_OPERATION = "knowledge_update_batch_v1"
NATIVE_RANK_LEARNING_OPERATION = "rank_learning_v1"
NATIVE_VECTOR_PROFILE_OPERATION = "document_vector_profiles_v1"

# Exact contract available through the private owner-scoped broker. Global
# maintenance and code-index tools remain app-only.
BROKER_MEMORY_TOOLS = frozenset(
    {
        "code_query",
        "capture",
        "delete_memory",
        "digest",
        "get_memory",
        "handler_display_label",
        "graph_walk",
        "ingest_authored",
        "list_candidates",
        "list_memories",
        "list_quarantine",
        "mark_conversation_source_removed",
        "memory_explain",
        "memory_export",
        "memory_forget",
        "memory_quality",
        "memory_retention",
        "recall",
        "versioned_detail",
        "versioned_transition",
        "assign_trust",
        "get_trust",
        "resolve_question",
        "record_memory_access",
        "reopen_memory",
        "resolve_memory",
        "review_candidate",
        "search",
        "update_candidate",
        "update_memory",
    }
)


def _render_digest_identity(digest: Dict[str, Any], handler_label: str) -> None:
    """Render %USER% tokens in a digest payload's display fields, in place.

    The digest is a read-only projection rebuilt per call, so rendering its
    output never touches stored claims.  Any rendered field keeps a ``raw_*``
    companion so edit surfaces can round-trip the stored form.
    """
    if not handler_label:
        return

    def render_field(entry: Any, key: str) -> None:
        if not isinstance(entry, dict):
            return
        value = entry.get(key)
        if not isinstance(value, str) or "%" not in value:
            return
        from services.memory.principal_context import render_identity_template

        rendered = render_identity_template(value, handler_label=handler_label)
        if rendered != value:
            entry[key] = rendered
            entry[f"raw_{key}"] = value

    for pinned in digest.get("pinned") or []:
        render_field(pinned, "headline")
        render_field(pinned, "content")
    for question in digest.get("open_questions") or []:
        render_field(question, "content")
    for recent in digest.get("recent") or []:
        render_field(recent, "topic")


def _legacy_overlay_rows(
    db_path: str, owner: str, workspace_id: str, ids: List[str]
) -> List[Dict[str, Any]]:
    """Read curated rows in the exact wire shape ``get_memory`` emits.

    Mirrors the Rust store's curated projection (same filters: owner,
    unarchived, workspace-or-global) so overlay reads batch into one
    indexed query instead of paying one transport round-trip per record.
    A missing table or unreadable store yields no rows — callers treat
    that like the legacy row being absent.
    """
    import sqlite3

    placeholders = ",".join("?" for _ in ids)
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id,content,kind,priority,trust_score,confidence_score,"
                "importance_score,scene_name,source,source_type,owner,workspace_id,"
                "session_key,session_id,tags,source_message_ids,timestamps,"
                "created_at,updated_at,archived,last_accessed_at,exempt_from_decay,"
                "exempt_from_dedup,metadata,workspace_path FROM curated "
                "WHERE owner=? AND archived=0 "
                "AND (workspace_id=? OR workspace_id='global') "
                f"AND id IN ({placeholders})",
                (owner, workspace_id, *ids),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    result = []
    for row in rows:
        data = dict(row)
        for key in ("tags", "source_message_ids", "timestamps"):
            try:
                parsed = json.loads(data.get(key) or "[]")
            except (TypeError, ValueError):
                parsed = []
            data[key] = parsed if isinstance(parsed, list) else []
        try:
            metadata = json.loads(data.get("metadata") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        data["metadata"] = metadata if isinstance(metadata, dict) else {}
        for key in ("archived", "exempt_from_decay", "exempt_from_dedup"):
            data[key] = bool(data.get(key))
        result.append(data)
    return result


def _live_legacy_ids(db_path: str, owner: str, workspace_id: str) -> set[str]:
    """IDs of live legacy curated rows for the scope, in one indexed query.

    Replaces the ``list_memories`` transport call used for legacy/v2
    question reconciliation: the transport clamps page sizes, so its
    result was silently a truncated subset; this SQL is the correct
    superset and costs one index scan instead of a full wire page.
    """
    import sqlite3

    with sqlite3.connect(db_path, timeout=30) as conn:
        rows = conn.execute(
            "SELECT id FROM curated WHERE owner=? AND archived=0 "
            "AND (workspace_id=? OR workspace_id='global')",
            (owner, workspace_id),
        ).fetchall()
    return {str(row[0]) for row in rows}


_SKILL_DRAFT_WRITES = frozenset(('skill_draft_retire','skill_draft_prepare','skill_draft_reserve','skill_draft_dispatch',
    'skill_draft_terminal','skill_draft_revoke','skill_draft_prepared_cancel'))
_SKILL_DRAFT_READS = frozenset(('skill_draft_retire_original','skill_draft_prepare_original','skill_draft_reserve_original',
    'skill_draft_original','skill_draft_result_original'))


def _skill_draft_envelope(payload, owner, workspace):
    """Scope and size-check the actual private original, not caller-supplied outer labels."""
    if type(payload) is not dict or set(payload)!={'phase','request'} or type(payload['request']) is not dict:
        raise ValueError('skill draft original unavailable')
    phase=payload['phase']; request=payload['request']
    if phase not in _SKILL_DRAFT_WRITES | _SKILL_DRAFT_READS: raise ValueError('skill draft phase unavailable')
    if phase in ('skill_draft_retire','skill_draft_retire_original'):
        if request.get('owner')!=owner: raise ValueError('skill draft scope changed')
        arguments={'owner':owner,'workspace_id':workspace,**payload}
        raw=json.dumps(arguments,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        if len(raw)>65536: raise ValueError('skill draft request too large')
        return arguments
    if phase in ('skill_draft_prepare','skill_draft_prepare_original','skill_draft_prepared_cancel'):
        original=request
    elif phase in ('skill_draft_reserve','skill_draft_reserve_original'):
        original=request.get('original')
    else:
        attempt=request.get('attempt') if phase=='skill_draft_terminal' else request
        reserve=attempt.get('reservation_original') if type(attempt) is dict else None
        original=reserve.get('original') if type(reserve) is dict else None
    if type(original) is not dict or original.get('owner')!=owner or original.get('workspace_id')!=workspace:
        raise ValueError('skill draft scope changed')
    arguments={'owner':owner,'workspace_id':workspace,**payload}
    raw=json.dumps(arguments,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(raw)>65536: raise ValueError('skill draft request too large')
    return arguments



_S05_RESERVE_WRITES = frozenset(('s05_reserve_prepare','s05_reserve'))
_S05_RESERVE_READS = frozenset(('s05_reserve_original','s05_reserve_readiness'))

def _s05_reserve_envelope(payload, owner, workspace):
    """Bind the actual private slot request and full owned frame to scope."""
    if (type(payload) is not dict or set(payload)!={'phase','request'}
            or payload.get('phase') not in _S05_RESERVE_WRITES | _S05_RESERVE_READS
            or type(payload.get('request')) is not dict):
        raise ValueError('S05 original unavailable')
    request=payload['request']
    original=request.get('original') if payload['phase']=='s05_reserve' else request
    if type(original) is not dict or original.get('owner')!=owner or original.get('workspace_id')!=workspace:
        raise ValueError('S05 scope changed')
    arguments={'owner':owner,'workspace_id':workspace,**payload}
    raw=json.dumps(arguments,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(raw)>65536: raise ValueError('S05 request too large')
    return arguments


class MemeryProvider(MemoryProvider):
    """Scoped application memory over the private Memery worker pipe."""

    provider_id = "memery"
    display_name = "Memery"

    def __init__(
        self,
        command: Optional[str] = None,
        workspace_id: str = "",
        env: Optional[Dict[str, str]] = None,
        broker_url: Optional[str] = None,
        broker_token: Optional[str] = None,
        guarded_v17: bool = False,
        guarded_preparation=None,
        host_worker=False,
    ):
        if guarded_v17 and guarded_preparation is None:
            raise ValueError("guarded memory requires host preparation")
        self._guarded_v17 = guarded_preparation is not None
        self._guarded_preparation = guarded_preparation
        self._host_worker = host_worker is True
        self._command = command or os.environ.get("OPEN_CLANK_MEMERY_WORKER", "memery-worker")
        self._env = {"FM_SCOPE_AUTHORITY": "trusted-caller", **(env or {})}
        self._env.pop("FM_STORE_PROFILE", None)
        # An authenticated process scope is a binding, not a hint. Resolve it
        # before constructing MemoryScope so a child cannot silently use the
        # host's default workspace when FM_WORKSPACE_ID was supplied.
        self._workspace_id = str(
            workspace_id or self._env.get("FM_WORKSPACE_ID") or chat_workspace()
        ).strip()
        # The Memery worker and the Python v2 projection must use the same
        # database.  Reading the process-wide constant here would leak test,
        # workspace, or per-instance providers into the live install.
        from src.constants import APP_DB, AUTH_FILE, FM_DB_PATH
        # Canonical FilePolicy uses APP_DB when the operator has no override.
        # Pin that same trusted path in the native child rather than letting
        # lifecycle requests fall back to a different/default authority.
        self._env.setdefault("OPEN_CLANK_AUTHORITY_DB_PATH", os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB)
        self._env.setdefault("OPEN_CLANK_AUTHORITY_AUTH_PATH", os.environ.get("OPEN_CLANK_AUTHORITY_AUTH_PATH") or AUTH_FILE)
        self._fm_db_path = str(self._env.get("FM_DB_PATH") or FM_DB_PATH)
        self._broker_url = (
            broker_url
            if broker_url is not None
            else os.environ.get("OPEN_CLANK_MEMORY_BROKER_URL", "")
        ).strip()
        self._broker_token = (
            broker_token
            if broker_token is not None
            else os.environ.get("OPEN_CLANK_MEMORY_BROKER_TOKEN", "")
        ).strip()
        self._broker_client = None
        self._broker_ready = False
        self._broker_init_lock = asyncio.Lock()
        self._broker_owner = memory_owner(self._env.get("FM_OWNER"))
        self._broker_workspace = str(
            self._env.get("FM_WORKSPACE_ID") or self._workspace_id
        ).strip()
        if self._broker_url:
            if self._guarded_v17:
                raise ValueError("guarded memory cannot use a broker")
            parsed = urlparse(self._broker_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or not self._broker_token
            ):
                raise ValueError(
                    "Memery broker must be authenticated plain HTTP on loopback"
                )
        if self._guarded_v17:
            from src.memery_database import GuardedStorePreparation
            if type(guarded_preparation) is not GuardedStorePreparation:
                raise ValueError("guarded memory preparation unavailable")
            guarded_preparation.assert_valid()
            if (os.path.realpath(self._fm_db_path) != guarded_preparation.database_path
                    or self._env.get("FM_DB_ID") != guarded_preparation.database_id):
                raise ValueError("guarded memory database binding changed")
        # One supervisor serializes authority checks and worker dispatch for this store.
        self._owner_task: Optional[asyncio.Task] = None
        self._requests: Optional[asyncio.Queue] = None
        self._ready: Optional[asyncio.Future] = None
        self._owner_rpc_busy = False
        self._s07_rpc_reserved = False
        self._bounded_write_reserved = False
        self._mirror_review_lock = asyncio.Lock()
        self._mirror_review_original = None
        self._paired_review_ticket = None
        self._s08_policy_review_ticket = None
        self._explicit_add_review_ticket = None
        self._native_memory_ops_ticket = None
        self._guarded_batch_forget_ticket = None
        self._native_memory_ops_witness = None
        self._import_review_ticket = None
        self._import_review_operation_lock = asyncio.Lock()
        self._owned_transport = None
        self._owned_generation = None
        self._reset_retirement_witness = None
        self._terminal_s08_witness = None
        self._helped_witness = None
        self._explicit_add_witness = None
        self._s08_policy_witness = None
        self._owned_poisoned = False
        # Shared by scoped shallow copies; epochs invalidate copies across handoff.
        self._reset_handoff_state = {'root':self, 'epoch':0, 'token':None, 'completed':None, 'queries':set(), 'original':None}
        self._reset_handoff_epoch = 0

    def _assert_reset_handoff_access(self, token=None):
        state=self._reset_handoff_state
        if (self._reset_handoff_epoch != state['epoch'] or (state['token'] is not None and token is not state['token'])
            or (state['token'] is None and token is not None)):
            raise MemoryRequestRejectedError('memory reset handoff unavailable')

    def _begin_original_reset_handoff(self, claim, frozen, *, barrier, witness):
        """Fence this shared owner for one frozen original reset; never issue it."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from services.memory.skill_lifecycle import locked
        from src.openclank.account_request_barrier import AccountRequestBarrier, _ResetDrainWitness
        from src.openclank.fm_reset_receipt import _identity, _request
        from src.openclank import memory_policy_projection
        from src import rag_singleton
        self._assert_reset_handoff_access()
        state=self._reset_handoff_state
        if (state['root'] is not self or state.get('completed') is not None or type(claim) is not MemoryResetClaim or type(frozen) is not dict
            or type(barrier) is not AccountRequestBarrier or type(witness) is not _ResetDrainWitness
            or getattr(claim._coordinator._reset_state,'account_request_barrier',None) is not barrier
            or witness.barrier is not barrier or witness.owner != barrier._owner(claim.owner)
            or not barrier.reset_witness_current(witness)):
            raise MemoryRequestRejectedError('reset handoff unavailable')
        claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
        self._assert_reset_retirement_ready()
        saved=json.loads(json.dumps(frozen,ensure_ascii=False,allow_nan=False))
        prep=self._guarded_preparation; transport=self._owned_transport; generation=self._owned_generation
        pending=self._explicit_add_review_ticket
        if type(pending) is dict:self._retire_explicit_add_noop(pending)
        imported=self._import_review_ticket
        if type(imported) is dict and type(imported.get('import_no_effect')) is dict:self._import_review_ticket=None
        if (set(saved) != {'version','database','request'} or type(saved.get('version')) is not int or saved['version'] != 1
            or _identity(prep) != saved['database'] or type(saved['request']) is not dict
            or saved['request'].get('account_id') != claim.account_id or saved['request'].get('operation_id') != claim.operation_id
            or saved['request'].get('owner') != self._scope(claim.owner).owner or saved['request'].get('workspace_id') != self._workspace_id
            or saved['request'].get('components') != ['ingest'] or 'selective_manifest_sha256' not in saved['request']
            or not self._host_bounded_write_available() or transport is None or not transport.current(generation)
            or self._owner_rpc_busy or self._s07_rpc_reserved or self._bounded_write_reserved or self._paired_review_ticket is not None or self._explicit_add_review_ticket is not None or self._import_review_ticket is not None or self._native_memory_ops_ticket is not None or self._guarded_batch_forget_ticket is not None
            or self._requests is None or not self._requests.empty()
            or self.__dict__.get('_code_query_pending_starts') or any(not t.done() for t in self.__dict__.get('_code_query_cleanups',()))
            or any(not t.done() for t in state['queries'])):
            raise MemoryRequestRejectedError('reset handoff unavailable')
        request=saved['request']
        checked=_request(owner=request['owner'],account_id=request['account_id'],operation_id=request['operation_id'],
            workspace_id=request['workspace_id'],components=request['components'],expected_counts=request['expected_counts'],
            queued_retired=False,selective_manifest_sha256=request['selective_manifest_sha256'])
        if checked != request: raise MemoryRequestRejectedError('reset handoff unavailable')
        with locked(os.fspath(claim._coordinator.lock_target)):
            row=claim._coordinator._load_journal().get(claim.operation_id)
            if type(row) is not dict or row.get('reset_native_frozen') != saved or row.get('reset_native_request') != saved['request']:
                raise MemoryRequestRejectedError('reset handoff unavailable')
            started=row.get('reset_native_started')
            if started is not None and type(started) is not bool: raise MemoryRequestRejectedError('reset handoff unavailable')
            may_issue=started is not True
        token=_OriginalResetHandoff(self,claim,saved,prep,transport,generation,transport.process,self._owner_task,may_issue,None,None,barrier,witness)
        state['token']=token; state['epoch']+=1; self._reset_handoff_epoch=state['epoch'];token.epoch=state['epoch']
        try:
            token.policy=memory_policy_projection.begin_reset_preparation_handoff(prep)
            token.rag=rag_singleton.begin_reset_preparation_handoff(prep)
            claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
            if not barrier.reset_witness_current(witness): raise MemoryRequestRejectedError('reset drain changed')
            return token
        except BaseException:
            try:
                if token.rag is not None: rag_singleton.abort_reset_preparation_handoff(token.rag)
                if token.policy is not None: memory_policy_projection.abort_reset_preparation_handoff(token.policy)
            except BaseException:
                # Unknown lock custody remains fenced; never expose mixed state.
                raise
            state['token']=None
            raise

    def _resume_original_reset_handoff(self, claim, frozen, *, barrier, witness):
        """Rebind only the in-process original token to a freshly drained claim."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.account_request_barrier import AccountRequestBarrier, _ResetDrainWitness
        token=self._reset_handoff_state.get('token')
        self._assert_reset_handoff_access(token)
        if (type(token) is not _OriginalResetHandoff or token.provider is not self
            or type(claim) is not MemoryResetClaim or type(barrier) is not AccountRequestBarrier
            or type(witness) is not _ResetDrainWitness or token.barrier is not barrier
            or token.claim._coordinator is not claim._coordinator
            or (token.claim.owner,token.claim.account_id,token.claim.operation_id)!=(claim.owner,claim.account_id,claim.operation_id)
            or getattr(claim._coordinator._reset_state,'account_request_barrier',None) is not barrier
            or witness.barrier is not barrier or witness.owner!=barrier._owner(claim.owner)
            or barrier.reset_witness_current(witness) is not True or type(frozen) is not dict or frozen!=token.frozen
            or (token.task is not None and not token.task.done())
            or not token.issue_reserved or token.issue_future is None or not token.issue_future.done()
            or type(token.issue_ticket) is not dict or token.issue_ticket.get('issued') is not True
            or self._owner_rpc_busy or self._bounded_write_reserved or self._s07_rpc_reserved
            or (self._requests is not None and not self._requests.empty())):
            raise MemoryRequestRejectedError('original reset recovery unavailable')
        if (claim.assert_current_post_dispatch(provider=self,frozen=token.frozen) is not True
            or token.claim.assert_current_post_dispatch(provider=self,frozen=token.frozen) is not True):
            raise MemoryRequestRejectedError('original reset recovery unavailable')
        # Preserve the original claim and epoch; the durable started claim can
        # authorize receipt-only recovery, never another issue.
        token.may_issue=False
        token.witness=witness
        return token

    def _assert_original_reset_handoff(self, token):
        self._assert_reset_handoff_access(token)
        if (type(token) is not _OriginalResetHandoff or token.provider is not self or self._reset_handoff_state['root'] is not self
            or getattr(token.claim._coordinator._reset_state,'account_request_barrier',None) is not token.barrier
            or not token.barrier.reset_witness_current(token.witness)):
            raise MemoryRequestRejectedError('reset handoff unavailable')
        return token

    def _reserve_original_reset_issue(self, token, owner, workspace, payload, future, ticket):
        """Consume the in-process original permit before its sole queue insertion."""
        self._assert_original_reset_handoff(token)
        request=token.frozen['request']
        if (not token.may_issue or token.issue_reserved or token.task is not None
            or owner != request['owner'] or workspace != request['workspace_id']
            or payload != {'phase':'guarded_reset_commit', **request}):
            raise MemoryRequestRejectedError('original reset issue unavailable')
        token.issue_reserved=True;token.issue_future=future;token.issue_ticket=ticket;token.issue_drained=asyncio.Event()
        def current():
            from services.memory.skill_lifecycle import locked
            try:
                if not token.may_issue: return False
                self._assert_original_reset_handoff(token)
                self._assert_reset_retirement_ready(token.reset_witness)
                token.claim.assert_current(owner=token.claim.owner,account_id=token.claim.account_id,provider=self,operation_id=token.claim.operation_id)
                with locked(os.fspath(token.claim._coordinator.lock_target)):
                    row=token.claim._coordinator._load_journal().get(token.claim.operation_id)
                    return bool(type(row) is dict and row.get('reset_native_started') is True
                        and row.get('reset_native_frozen') == token.frozen and row.get('reset_native_request') == request)
            except Exception: return False
        return current

    def _mark_original_reset_unsent_drained(self, future):
        ticket=self._reset_handoff_state.get('original')
        if (type(ticket) is dict and ticket.get('reset_future') is future
            and ticket.get('issued') is False):
            # Called only by the owner before arming, or by the writer after
            # suppressing this exact frame; synchronously revoke any later send.
            ticket['may_issue']=False
            ticket['reset_unsent_drained']=True
        token=getattr(future,'_reset_handoff_token',None)
        if (type(token) is _OriginalResetHandoff and token.issue_future is future
            and type(token.issue_ticket) is dict and token.issue_ticket.get('issued') is False
            and token.issue_drained is not None):
            token.issue_drained.set()

    async def _abort_original_reset_handoff(self, token):
        """Release unchanged witnesses only after the exact original was unsent."""
        from src.openclank import memory_policy_projection
        from src import rag_singleton
        from src.openclank.mimo_supervisor import _queued_owned_thread
        self._assert_original_reset_handoff(token)
        if not token.may_issue or token.task is not None or token.receipt is not None or token.new_preparation is not None:
            raise MemoryRequestRejectedError('reset handoff outcome unknown')
        future=token.issue_future;ticket=token.issue_ticket
        if token.issue_reserved:
            if future is None or type(ticket) is not dict or token.issue_drained is None or ticket.get('issued') is not False:
                raise MemoryRequestRejectedError('reset handoff outcome unknown')
            if not future.done(): future.cancel()
            waiter=asyncio.create_task(token.issue_drained.wait())
            try:
                done,_=await asyncio.wait((waiter,token.owner_task),return_when=asyncio.FIRST_COMPLETED)
                if waiter not in done or not token.issue_drained.is_set():
                    raise MemoryRequestRejectedError('reset handoff outcome unknown')
            finally:
                if not waiter.done(): waiter.cancel()
        elif future is not None or ticket is not None:
            raise MemoryRequestRejectedError('reset handoff outcome unknown')
        self._assert_original_reset_handoff(token)
        token.claim.assert_current(owner=token.claim.owner,account_id=token.claim.account_id,provider=self,operation_id=token.claim.operation_id)
        if ((ticket is not None and ticket.get('issued') is not False) or self._owned_transport is not token.transport
            or self._owned_generation is not token.generation or token.transport.process is not token.process
            or self._owner_task is not token.owner_task or self._guarded_preparation is not token.old_preparation
            or self._owner_rpc_busy or self._bounded_write_reserved or self._requests is None or not self._requests.empty()):
            raise MemoryRequestRejectedError('reset handoff outcome unknown')
        await _queued_owned_thread(self._assert_reset_retirement_ready,token.reset_witness)
        self._assert_original_reset_handoff(token)
        token.claim.assert_current(owner=token.claim.owner,account_id=token.claim.account_id,provider=self,operation_id=token.claim.operation_id)
        if (not token.may_issue or token.task is not None or token.receipt is not None or token.new_preparation is not None
            or self._owned_transport is not token.transport or self._owned_generation is not token.generation
            or token.transport.process is not token.process or self._guarded_preparation is not token.old_preparation
            or self._owner_task is not token.owner_task or self._owner_rpc_busy or self._bounded_write_reserved
            or self._requests is None or not self._requests.empty()
            or (ticket is not None and ticket.get('issued') is not False)):
            raise MemoryRequestRejectedError('reset handoff outcome unknown')
        # No await between unchanged-witness abort and retirement of this token.
        rag_singleton.abort_reset_preparation_handoff(token.rag)
        memory_policy_projection.abort_reset_preparation_handoff(token.policy)
        token.may_issue=False
        fact=_OriginalResetUnsent(_ORIGINAL_RESET_UNSENT_SEAL,self,token);token.unsent_fact=fact
        self._reset_handoff_state['token']=None
        return fact

    def _assert_original_reset_unsent(self, fact, claim):
        """Validate the provider-produced historical fact under its claim."""
        if (type(fact) is not _OriginalResetUnsent or fact._seal is not _ORIGINAL_RESET_UNSENT_SEAL
            or fact.provider is not self or type(fact.token) is not _OriginalResetHandoff
            or fact.token.unsent_fact is not fact or fact.token.claim is not claim
            or fact.token.may_issue is not False or fact.token.task is not None
            or self._reset_handoff_state['token'] is not None or self._reset_handoff_state['epoch']!=fact.token.epoch):
            raise MemoryRequestRejectedError('original unsent fact unavailable')
        token=fact.token
        if not token.issue_reserved and (token.issue_future is not None or token.issue_ticket is not None):
            raise MemoryRequestRejectedError('original unsent fact unavailable')
        if token.issue_reserved and (token.issue_future is None or not token.issue_future.done()
            or type(token.issue_ticket) is not dict or token.issue_ticket.get('issued') is not False
            or token.issue_drained is None or not token.issue_drained.is_set()):
            raise MemoryRequestRejectedError('original unsent fact unavailable')
        claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
        return fact

    async def _run_original_reset_handoff(self, token, current):
        """Read exact commit, retire its child and stage one independently prepared generation."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.fm_reset_receipt import read_committed_reset_receipt, _identity
        from src.memery_database import prepare_guarded_memery_database
        self._assert_original_reset_handoff(token)
        if getattr(current,'__self__',None) is not token.claim or getattr(current,'__func__',None) is not MemoryResetClaim.assert_current_post_dispatch:
            raise MemoryRequestRejectedError('reset handoff claim unavailable')
        def check():
            try:
                self._assert_original_reset_handoff(token)
                return current(provider=self,frozen=token.frozen) is True
            except Exception: return False
        async def owned_thread(fn,*args,**kwargs):
            task=asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs))
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            return task.result()
        async def retire(transport, generation, process, owner_task):
            # Never substitute a later owner or assume an unobserved spawn died.
            if (transport is None or process is None or owner_task is None
                or self._owned_transport is not transport or self._owned_generation is not generation
                or transport.process is not process
                or self._owner_task not in (owner_task, None)):
                raise MemoryRequestRejectedError('memory child custody unavailable')
            if self._owner_task is owner_task:
                await self.shutdown(_reset_handoff_token=token)
            elif not owner_task.done() or self._requests is not None:
                raise MemoryRequestRejectedError('memory child custody unavailable')
            confirmation=transport.exit_confirmation(generation)
            if not confirmation.done():
                raise MemoryRequestRejectedError('memory child exit unavailable')
            exit_code=confirmation.result()
            if (type(exit_code) is not int or process.returncode != exit_code or not owner_task.done()
                or self._requests is not None or self._owner_task is not None
                or self._bounded_write_reserved or self._s07_rpc_reserved or self._owner_rpc_busy or not check()):
                raise MemoryRequestRejectedError('memory child unsettled')

        async def run():
            if not check(): raise MemoryRequestRejectedError('reset handoff claim unavailable')
            if token.receipt is None:
                receipt=await owned_thread(read_committed_reset_receipt,token.old_preparation,token.frozen,current=check)
                if receipt.get('state') != 'committed' or receipt.get('cleanup') != 'unproved':
                    raise MemoryRequestRejectedError('reset receipt unavailable')
                token.receipt=receipt
            receipt=token.receipt
            if not token.original_retired:
                await self._host_guarded_reset_stamp(claim=token.claim,frozen=token.frozen,receipt=receipt,current=check,_reset_handoff_token=token)
                await retire(token.transport,token.generation,token.process,token.owner_task)
                token.original_retired=True
            if token.attempt is not None:
                await retire(*token.attempt)
                token.attempt=None
            fresh=await owned_thread(prepare_guarded_memery_database,token.frozen['database']['path'],command=self._command,expected_database_id=token.frozen['database']['database_id'],trusted_env=dict(self._env),profile=('guarded-v18' if token.old_preparation.schema_version == 18 else 'guarded-v17'))
            if (_identity(fresh) != token.frozen['database'] or dict(fresh.capabilities) != dict(token.old_preparation.capabilities)
                or fresh._schema_fingerprint != token.old_preparation._schema_fingerprint or not check()):
                raise MemoryRequestRejectedError('new memory preparation changed')
            self._guarded_preparation=fresh; self._owned_poisoned=False; self._code_query_closing=False
            self._reset_retirement_witness=None; self._terminal_s08_witness=None; self._helped_witness=None; self._explicit_add_witness=None; self._s08_policy_witness=None
            try:
                await self.initialize(_reset_handoff_token=token)
            finally:
                # Retain exact attempted custody even when handshake failed.
                transport=self._owned_transport
                token.attempt=(transport,self._owned_generation,getattr(transport,'process',None),self._owner_task)
            if not self._host_bounded_write_available() or self._owned_transport is token.transport or not check():
                raise MemoryRequestRejectedError('new memory owner unavailable')
            again=await owned_thread(read_committed_reset_receipt,fresh,token.frozen,current=check)
            if again != receipt: raise MemoryRequestRejectedError('reset receipt changed')
            if token.frozen['request']['components'] != ['ingest']: raise MemoryRequestRejectedError('reset cleanup unavailable')
            token.new_preparation=fresh; token.cleanup_proved=True
            return {'state':'prepared_pending_handoff','receipt':receipt,'old_preparation':token.old_preparation,'new_preparation':fresh}
        if token.task is None or (token.task.done() and not token.task.cancelled() and token.task.exception() is not None):
            token.task=asyncio.create_task(run())
        cancelled=False
        while not token.task.done():
            try: await asyncio.shield(token.task)
            except asyncio.CancelledError: cancelled=True
        if cancelled:
            try: token.task.result()
            except BaseException: pass
            raise asyncio.CancelledError
        return token.task.result()

    def _rebind_original_reset_consumers(self, token, current):
        """Rebind the exact held policy/RAG witnesses without yielding."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank import memory_policy_projection
        from src import rag_singleton
        self._assert_original_reset_handoff(token)
        if (getattr(current,'__self__',None) is not token.claim or getattr(current,'__func__',None) is not MemoryResetClaim.assert_current_post_dispatch
            or current(provider=self,frozen=token.frozen) is not True or not token.cleanup_proved
            or token.new_preparation is None or self._guarded_preparation is not token.new_preparation):
            raise MemoryRequestRejectedError('reset handoff incomplete')
        if not token.policy_rebound:
            memory_policy_projection.rebind_reset_preparation_handoff(token.policy,token.new_preparation);token.policy_rebound=True
        if not token.rag_rebound:
            rag_singleton.rebind_reset_preparation_handoff(token.rag,token.new_preparation);token.rag_rebound=True
        return True

    def _finish_original_reset_handoff(self, token, current):
        """Release only after exact consumer references have been rebound."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank import memory_policy_projection
        from src import rag_singleton
        self._assert_original_reset_handoff(token)
        if (getattr(current,'__self__',None) is not token.claim or getattr(current,'__func__',None) is not MemoryResetClaim.assert_current_post_dispatch
            or current(provider=self,frozen=token.frozen) is not True or token.task is None or not token.task.done()
            or token.task.cancelled() or token.task.exception() is not None or not token.cleanup_proved
            or token.new_preparation is None or self._guarded_preparation is not token.new_preparation
            or not self._host_bounded_write_available() or not token.policy_rebound or not token.rag_rebound):
            raise MemoryRequestRejectedError('reset handoff incomplete')
        # Keep the RAG initializer fenced until the final non-yielding release.
        if not token.policy_finished:
            memory_policy_projection.finish_reset_preparation_handoff(token.policy);token.policy_finished=True
        if (memory_policy_projection.policy_coordinator() is not token.policy.coordinator
            or (token.policy.projection is not None and token.policy.projection._preparation is not token.new_preparation)):
            raise MemoryRequestRejectedError('reset handoff policy changed')
        if not token.rag_finished:
            rag_singleton.finish_reset_preparation_handoff(token.rag);token.rag_finished=True
        # Keep one exact completed original until its final journal row is durable.
        if self._reset_handoff_state.get('completed') is not None:
            raise MemoryRequestRejectedError('completed reset custody unavailable')
        self._reset_handoff_state['completed']=token
        self._reset_handoff_state['token']=None
        return True

    def _finished_original_reset_handoff(self, claim, frozen, *, barrier, witness):
        from src.openclank.account_request_barrier import AccountRequestBarrier, _ResetDrainWitness
        from src.openclank.fm_reset_receipt import _identity
        from src.openclank import memory_policy_projection
        from src import rag_singleton
        token=self._reset_handoff_state.get('completed')
        if (type(token) is not _OriginalResetHandoff or token.provider is not self or type(claim) is not type(token.claim)
            or claim._coordinator is not token.claim._coordinator or (claim.owner,claim.account_id,claim.operation_id)!=(token.claim.owner,token.claim.account_id,token.claim.operation_id)
            or frozen!=token.frozen or type(barrier) is not AccountRequestBarrier or type(witness) is not _ResetDrainWitness
            or barrier is not token.barrier or witness.barrier is not barrier or witness.owner!=barrier._owner(claim.owner)
            or getattr(claim._coordinator._reset_state,'account_request_barrier',None) is not barrier
            or barrier.reset_witness_current(witness) is not True or claim.assert_current_post_dispatch(provider=self,frozen=frozen) is not True
            or token.claim.assert_current_post_dispatch(provider=self,frozen=frozen) is not True
            or not token.cleanup_proved or not token.policy_finished or not token.rag_finished or token.receipt is None
            or self._guarded_preparation is not token.new_preparation or _identity(token.new_preparation)!=frozen['database']
            or not token.policy.released or token.policy.new is not token.new_preparation or not token.rag.released or token.rag.new is not token.new_preparation
            or memory_policy_projection.policy_coordinator() is not token.policy.coordinator or rag_singleton.rag_instance is not token.rag.manager
            or (token.policy.projection is not None and token.policy.projection._preparation is not token.new_preparation)
            or (token.rag.manager is not None and token.rag.manager._guarded_preparation is not token.new_preparation)):
            raise MemoryRequestRejectedError('finished reset custody unavailable')
        token.new_preparation.assert_valid()
        return {'state':'finished','receipt':token.receipt}

    def _ack_finished_original_reset_handoff(self, claim, frozen, result):
        from services.memory.skill_lifecycle import locked
        from src.openclank.fm_reset_receipt import _identity
        token=self._reset_handoff_state.get('completed')
        if token is None: return True
        if (type(token) is not _OriginalResetHandoff or token.provider is not self or type(claim) is not type(token.claim)
            or claim._coordinator is not token.claim._coordinator or (claim.owner,claim.account_id,claim.operation_id)!=(token.claim.owner,token.claim.account_id,token.claim.operation_id)
            or token.frozen!=frozen or type(result) is not dict or result.get('complete') is not True
            or result.get('status')!='complete' or result.get('operation_id')!=claim.operation_id
            or type(result.get('categories')) is not dict or type(result['categories'].get('ingest')) is not dict
            or result['categories']['ingest'].get('state')!='complete' or self._guarded_preparation is not token.new_preparation
            or _identity(token.new_preparation)!=frozen['database'] or not token.policy_finished or not token.rag_finished):
            raise MemoryRequestRejectedError('finished reset acknowledgment unavailable')
        coordinator=claim._coordinator
        auth=getattr(coordinator,'_reset_auth',None)
        from src.openclank.chat_source_bridge import canonical_source_provider
        if (auth is None or auth.account_id(claim.owner)!=claim.account_id or auth.username_for_account_id(claim.account_id)!=claim.owner
            or canonical_source_provider(getattr(coordinator,'_reset_state',None)) is not self):
            raise MemoryRequestRejectedError('finished reset account changed')
        with locked(os.fspath(coordinator.lock_target)):
            row=coordinator._load_journal().get(claim.operation_id)
            if (type(row) is not dict or row.get('state')!='complete' or row.get('result')!=result
                or row.get('owner')!=claim.owner or row.get('account_id')!=claim.account_id
                or row.get('provider_owner')!=self._scope(claim.owner).owner
                or row.get('reset_native_frozen')!=frozen or row.get('reset_native_request')!=frozen['request']
                or row.get('reset_selective_manifest_sha256')!=frozen['request'].get('selective_manifest_sha256')
                or type(row.get('reset_local_steps')) is not dict or type(row['reset_local_steps'].get('staging')) is not dict
                or row['reset_local_steps']['staging'].get('state')!='complete'
                or row.get('reset_native_result')!=token.receipt.get('sql_result') or row.get('reset_selective_native_complete') is not True):
                raise MemoryRequestRejectedError('finished reset journal unavailable')
        if self._reset_handoff_state.get('completed') is not token: raise MemoryRequestRejectedError('finished reset custody changed')
        self._reset_handoff_state['completed']=None
        return True

    def _local_db_path(self) -> Optional[str]:
        """Only the server may project the native store directly."""
        if self._broker_url or not self._host_worker:
            return None
        return self._fm_db_path

    def persisted_database_identity(self) -> dict[str, str]:
        """Read the explicitly pinned local identity without starting a provider."""
        from contextlib import closing
        from pathlib import Path
        path = self._env.get("FM_DB_PATH")
        database_id = self._env.get("FM_DB_ID")
        if (self._broker_url or not self._host_worker or type(path) is not str or not path or type(database_id) is not str
                or not database_id or database_id != database_id.strip()):
            raise MemoryRequestRejectedError("persisted memory identity unavailable")
        canonical = os.path.realpath(os.path.abspath(path))
        try:
            if self._guarded_v17:
                self._guarded_preparation.assert_valid()
                if (canonical != self._guarded_preparation.database_path
                        or database_id != self._guarded_preparation.database_id):
                    raise ValueError("guarded identity changed")
            before = os.stat(canonical)
            with closing(sqlite3.connect(Path(canonical).as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
            after = os.stat(canonical)
            if (row is None or row[0] != database_id
                    or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
                raise ValueError("persisted identity changed")
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise MemoryRequestRejectedError("persisted memory identity unavailable") from exc
        return {"database_id":database_id, "database_path":canonical}

    def broker_health(self) -> Dict[str, Any]:
        """Scope-free readiness for broker clients, from this provider's own connection state (no Native call)."""
        ready = self._ready
        ok = bool(not self._broker_url and not self._owned_poisoned and self._owner_task and not self._owner_task.done()
                  and ready is not None and ready.done() and not ready.cancelled() and ready.exception() is None
                  and self._owned_transport is not None and self._owned_transport.current(self._owned_generation))
        return {"ready": ok, "profile": "guarded" if self._guarded_v17 else "legacy"}

    async def initialize(self, *, _reset_handoff_token=None) -> None:
        self._assert_reset_handoff_access(_reset_handoff_token)
        if self._broker_url:
            async with self._broker_init_lock:
                if self._broker_ready:
                    return
                import httpx

                self._broker_client = httpx.AsyncClient(timeout=45.0)
                try:
                    health_data = await self._broker_request(
                        "memory_quality", {"rebuild_graph_fts": False}
                    )
                    if type(health_data) is not dict or health_data.get("ready") is not True:
                        raise RuntimeError("Memery broker is not ready")
                except BaseException:
                    await self._broker_client.aclose()
                    self._broker_client = None
                    raise
                self._broker_ready = True
                logger.info("MemeryProvider connected to Open Clank memory broker")
                return
        if not self._host_worker:
            raise MemoryTransportError("Memery requires an authenticated host connection")
        if self._owned_poisoned:
            raise MemoryTransportError('guarded memory transport generation is unavailable')
        if self._owner_task and not self._owner_task.done():
            await asyncio.shield(self._ready)
            self._assert_reset_handoff_access(_reset_handoff_token)
            if self._owned_poisoned:
                raise MemoryTransportError('guarded memory transport generation is unavailable')
            return
        loop = asyncio.get_running_loop()
        self._owner_rpc_busy = False
        self._s07_rpc_reserved = False
        self._bounded_write_reserved = False
        self._s08_policy_witness = None
        self._owned_generation = object()
        self._requests = asyncio.Queue(maxsize=128)
        self._ready = loop.create_future()
        ready = self._ready
        task = asyncio.create_task(self._owner_loop(), name="memery-worker-owner")
        self._owner_task = task
        def settle_unready_owner(done):
            # Resolve this task's captured future even if a replacement has
            # already installed its own task and readiness future.
            if done is not task:
                return
            if ready.done():
                if not done.cancelled(): done.exception()
                return
            if done.cancelled():
                error = MemoryTransportError("Memery owner stopped before readiness")
            else:
                error = done.exception() or MemoryTransportError("Memery owner stopped before readiness")
            ready.set_exception(error)
        task.add_done_callback(settle_unready_owner)
        await asyncio.shield(ready)
        self._assert_reset_handoff_access(_reset_handoff_token)
        if self._owned_poisoned:
            raise MemoryTransportError('guarded memory transport generation is unavailable')

    async def _owner_loop(self) -> None:
        from src.openclank.fm_owned_stdio import OwnedStdio

        requests = self._requests
        issued_ticket = None
        active_future = None
        maintenance = None
        try:
            async with AsyncExitStack() as stack:
                server_env = {**os.environ, **(self._env or {})}
                # Never inherit host-review authority into agent/broker children.
                server_env.pop("FM_HOST_PRIVATE_REVIEW", None)
                server_env.pop("FM_STORE_PROFILE", None)
                if self._guarded_v17 and not self._broker_url:
                    self._guarded_preparation.assert_valid()
                    server_env["FM_HOST_PRIVATE_REVIEW"] = "1"
                    server_env["FM_STORE_PROFILE"] = "guarded-v18" if self._guarded_preparation.schema_version == 18 else "guarded-v17"
                # Provider-bearing embedding environment shims were removed
                # by the managed-provider hard cut.  Memery retains
                # exact/FTS memory when its unfingerprinted vector schema
                # cannot safely consume managed embeddings.
                for name in tuple(server_env):
                    if name.startswith("FM_EMBED_"):
                        server_env.pop(name, None)
                # A database path and its identity are one atomic binding.
                # Callers that deliberately select another database must not
                # inherit the identity of the process-wide production store.
                if "FM_DB_PATH" in self._env and "FM_DB_ID" not in self._env:
                    server_env.pop("FM_DB_ID", None)
                server_env["OPEN_CLANK_MEMERY_DB_PATH"] = self._fm_db_path
                server_env["FM_DB_PATH"] = self._fm_db_path
                from src.constants import DATA_DIR
                log_path = os.path.join(DATA_DIR, "logs", "memery-worker.stderr.log")
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                errlog = stack.enter_context(open(log_path, "a", encoding="utf-8"))
                owned = OwnedStdio(self._owned_generation)
                self._owned_transport = owned
                health_data = await stack.enter_async_context(owned.open(self._command, server_env, errlog=errlog))
                if (health_data.get("database_id") != server_env.get("FM_DB_ID")
                        or health_data.get("database_path") != self._fm_db_path
                        or type(health_data.get("schema_version")) is not int):
                    raise MemoryTransportError("Memery worker opened a different store")
                if not self._guarded_v17 and health_data["schema_version"] != 16:
                    raise MemoryTransportError("Memery legacy schema mismatch")
                if self._guarded_v17:
                    prepared = self._guarded_preparation
                    prepared.assert_valid()
                    expected = {"database_id":prepared.database_id, "schema_version":prepared.schema_version,
                        "database_path":prepared.database_path, "profile":"guarded-v18" if prepared.schema_version == 18 else "guarded-v17",
                        "device":prepared.device, "inode":prepared.inode, "schema_cookie":prepared.schema_cookie,
                        "capabilities":dict(prepared.capabilities)}
                    caps = health_data.get("capabilities") if type(health_data) is dict else None
                    if (type(health_data) is not dict
                            or set(health_data) != set(expected) or type(health_data.get("schema_version")) is not int
                            or type(health_data.get("device")) is not int or type(health_data.get("inode")) is not int
                            or type(caps) is not dict or set(caps) not in (
                                {"guarded_review", "source_lifecycle", "feedback", "admitted", "source_erase", "owner_transition", "assistant_interpretation"},
                                {"guarded_review", "source_lifecycle", "feedback", "admitted", "source_erase", "owner_transition", "assistant_interpretation", "account_chat"},
                                {"guarded_review", "source_lifecycle", "feedback", "admitted", "source_erase", "owner_transition", "assistant_interpretation", "account_chat", "document_fts"},
                                {"guarded_review", "source_lifecycle", "feedback", "admitted", "source_erase", "owner_transition", "assistant_interpretation", "account_chat", "document_fts", "document_import_v1"})
                            or any(type(value) is not bool for value in caps.values()) or health_data != expected):
                        raise RuntimeError("guarded memory profile handshake mismatch")
                # Restore global indexing configuration during this normal
                # owner handshake. An old engine may still serve memory; code
                # admission separately requires an acknowledged scheduler.
                if not self._guarded_v17:
                    try:
                        from src.settings import load_settings
                        from src.openclank.code_index_settings import configuration_request, validate_status, configured, configured_limits
                        saved = load_settings()
                        # Bound only this optional preference acknowledgment. A
                        # stalled/old engine must not prevent ordinary memory
                        # readiness; actual indexing requests have no deadline.
                        restored, _ = await owned.call(
                            "code_index_scheduler",
                            {"origin": "app", **configuration_request(saved)},
                            timeout=1.5,
                        )
                        status = validate_status(restored)
                        requested_limits = configured_limits(saved)
                        if ((status["desired"], status["revision"]) != configured(saved)
                                or status["desired_limits"] != requested_limits
                                or status["effective_limits"] != requested_limits):
                            raise ValueError("Indexing scheduler not acknowledged")
                    except Exception:
                        logger.warning("Indexing scheduler restore pending; ordinary memory remains available")
                logger.info("Memery worker connected")
                if owned.disconnected.done():
                    raise MemoryTransportError("Memery worker disconnected during startup")
                owned.initialized = True

                def wake_disconnected(_):
                    if requests.empty():
                        requests.put_nowait(None)

                owned.disconnected.add_done_callback(wake_disconnected)
                if not self._ready.done():
                    self._ready.set_result(None)
                while True:
                    item = await requests.get()
                    if item is None:
                        return
                    name, arguments, fut = item
                    active_future = fut
                    if owned.disconnected.done():
                        raise ConnectionError("Memery worker disconnected")
                    try: self._assert_reset_handoff_access(getattr(fut,'_reset_handoff_token',None))
                    except MemoryRequestRejectedError as exc:
                        if getattr(fut,'_owned_write_ticket',None) is not None: self._bounded_write_reserved=False
                        self._mark_original_reset_unsent_drained(fut)
                        skipped=getattr(fut,'_owned_write_ticket',None)
                        if type(skipped) is dict and skipped.get('owned_original_payload') is not None and skipped.get('issued') is False:
                            skipped['may_issue']=False;skipped['owned_io_drained']=True
                        if getattr(fut,'_s07_deadline',None) is not None: self._s07_rpc_reserved=False
                        if not fut.done(): fut.set_exception(exc)
                        continue
                    handoff_check=getattr(fut,'_reset_handoff_check',None)
                    if handoff_check is not None and handoff_check() is not True:
                        self._bounded_write_reserved=False
                        self._mark_original_reset_unsent_drained(fut)
                        skipped=getattr(fut,'_owned_write_ticket',None)
                        if type(skipped) is dict and skipped.get('owned_original_payload') is not None and skipped.get('issued') is False:
                            skipped['may_issue']=False;skipped['owned_io_drained']=True
                        if not fut.done(): fut.set_exception(MemoryRequestRejectedError('original reset authority changed'))
                        continue
                    ticket = getattr(fut, '_owned_write_ticket', None)
                    read_ticket = getattr(fut, '_owned_read_ticket', None)
                    if self._owned_poisoned:
                        if not fut.done(): fut.set_exception(MemoryTransportError('guarded memory transport generation is unavailable'))
                        return
                    if ticket is not None and (fut.cancelled() or asyncio.get_running_loop().time() >= ticket['deadline']
                            or owned is not ticket['transport'] or not owned.current(ticket['generation'])):
                        self._bounded_write_reserved = False
                        self._mark_original_reset_unsent_drained(fut)
                        skipped=getattr(fut,'_owned_write_ticket',None)
                        if type(skipped) is dict and skipped.get('owned_original_payload') is not None and skipped.get('issued') is False:
                            skipped['may_issue']=False;skipped['owned_io_drained']=True
                        if not fut.done(): fut.set_exception(MemoryRequestRejectedError('bounded memory write unavailable'))
                        continue
                    queue_check = ticket.get('queue_before_issue',ticket.get('before_issue')) if ticket is not None else None
                    if queue_check is not None:
                        try:
                            current = queue_check() is True
                        except Exception:
                            current = False
                        if not current:
                            self._bounded_write_reserved = False
                            self._mark_original_reset_unsent_drained(fut)
                            skipped=getattr(fut,'_owned_write_ticket',None)
                            if type(skipped) is dict and skipped.get('owned_original_payload') is not None and skipped.get('issued') is False:
                                skipped['may_issue']=False;skipped['owned_io_drained']=True
                            if not fut.done(): fut.set_exception(MemoryRequestRejectedError('paired review authority changed'))
                            continue
                    s07_deadline = getattr(fut, '_s07_deadline', None)
                    s07_queued = getattr(fut, '_s07_debug_queued', None)
                    s07_owner_phase = arguments.get('phase') if s07_queued is not None and arguments.get('phase') in ('s07_recall_start','s07_recall_poll','s07_revalidate_start') else None
                    if s07_owner_phase:
                        logger.debug('S07_NATIVE phase=%s stage=owner_dequeue elapsed_ms=%.3f',s07_owner_phase,(asyncio.get_running_loop().time()-s07_queued)*1000)
                    if s07_deadline is not None and (fut.cancelled() or asyncio.get_running_loop().time() >= s07_deadline):
                        self._s07_rpc_reserved = False
                        if not fut.done(): fut.set_exception(MemoryRequestRejectedError('bounded recall unavailable'))
                        continue
                    if fut.cancelled() and ticket is None:
                        # A cancelled caller no longer needs the queued operation.
                        continue
                    # Guarded writes keep exclusive ownership of their generation.
                    # The legacy store can admit one background rebuild or ingestion
                    # beside interactive reads. Preserve ordering for all other writes.
                    readable = name in {"digest", "search", "recall", "get_memory", "list_memories",
                                        "memory_explain", "memory_export", "list_quarantine",
                                        "list_candidates", "graph_walk"} or (
                        name == "memory_quality" and not arguments.get("rebuild_graph_fts"))
                    background = not self._guarded_v17 and (
                        name == "ingest_authored" or (name == "memory_quality" and arguments.get("rebuild_graph_fts")))
                    if background:
                        if maintenance is not None and not maintenance.done():
                            fut.set_exception(MemoryRequestRejectedError("Memery maintenance is busy", code="busy"))
                            continue

                        async def run_maintenance(operation, payload, waiter):
                            try:
                                reply = await owned.call(operation, payload)
                            except Exception as exc:
                                if not waiter.done(): waiter.set_exception(exc)
                            else:
                                if not waiter.done(): waiter.set_result(reply)
                            finally:
                                if not waiter.done(): waiter.set_exception(ConnectionError("Memery maintenance stopped"))

                        maintenance = asyncio.create_task(run_maintenance(name, arguments, fut))
                        continue
                    if not readable and maintenance is not None:
                        await maintenance
                        maintenance = None
                    self._owner_rpc_busy = True
                    try:
                        with code_request_service(fut):
                            if s07_deadline is not None:
                                if read_ticket is not None:
                                    if owned is not read_ticket['transport'] or not owned.current(read_ticket['generation']):raise RuntimeError('owned read generation changed')
                                    owned.arm_read(read_ticket['generation'],name,arguments,read_ticket)
                                    read_ticket['issued']=True
                                remaining = s07_deadline - asyncio.get_running_loop().time()
                                if remaining <= 0: raise TimeoutError('bounded recall expired')
                                if arguments.get('phase') in {'s07_recall_start','s07_revalidate_start'}:
                                    arguments['budget_ms'] = max(1, min(200, int(remaining * 1000)))
                                if s07_owner_phase:
                                    logger.debug('S07_NATIVE phase=%s stage=worker_start elapsed_ms=%.3f',s07_owner_phase,(asyncio.get_running_loop().time()-s07_queued)*1000)
                                result = await owned.call(name, arguments, timeout=remaining)
                                if s07_owner_phase:
                                    logger.debug('S07_NATIVE phase=%s stage=worker_return elapsed_ms=%.3f',s07_owner_phase,(asyncio.get_running_loop().time()-s07_queued)*1000)
                            else:
                                options = {"timeout": 1.5} if name == "code_index_scheduler" else {}
                                if readable:
                                    options["caller"] = fut
                                if ticket is not None:
                                    owned.arm_write(ticket['generation'],name,arguments,ticket)
                                    issued_ticket = ticket
                                    options["timeout"] = max(0, ticket["deadline"] - asyncio.get_running_loop().time())
                                result = await owned.call(name, arguments, **options)
                    except BaseException as exc:
                        terminal_error=(ticket is not None and isinstance(exc,Exception)
                            and ticket.get('terminal_error') is True and not ticket.get('frame_invalid'))
                        if arguments.get('phase') in ('explicit_add','explicit_add_status','explicit_add_capability'):
                            bounded = ticket or read_ticket or {}
                            logger.warning("Explicit Add transport failure phase=%s error=%s issued=%s terminal=%s invalid_frame=%s",
                                           arguments['phase'], type(exc).__name__, bounded.get('issued'),
                                           bounded.get('frame_seen'), bounded.get('frame_invalid'))
                        if ticket is not None and ticket['issued'] and not terminal_error:
                            # A client exception/EOF does not prove the write
                            # stopped. Fence the generation before publishing
                            # the exception or admitting another queued call.
                            self._owned_poisoned = True
                            ticket['transport'].poisoned = True
                        if not isinstance(exc, Exception):
                            raise
                        if terminal_error:
                            issued_ticket=None
                        if not fut.done(): fut.set_exception(MemoryRequestRejectedError('bounded memory write rejected') if terminal_error else exc)
                    else:
                        if not fut.done(): fut.set_result(result)
                        if ticket is not None: issued_ticket = None
                    finally:
                        self._owner_rpc_busy = False
                        if ticket is not None:
                            if (ticket.get('owned_original_payload') is not None and not ticket.get('frame_invalid')
                                and (ticket.get('frame_seen') is True or (ticket.get('positive_unsent') is True and ticket.get('issued') is False))):
                                ticket['owned_io_drained']=True
                            owned.disarm_write(ticket)
                            self._bounded_write_reserved = False
                        if read_ticket is not None and read_ticket.get('issued'):
                            await self._settle_owned_read_ticket(owned,read_ticket)
                        if s07_deadline is not None: self._s07_rpc_reserved = False
                    if self._owned_poisoned:
                        return
        except BaseException as exc:
            if issued_ticket is not None and issued_ticket['issued']:
                # Fence restart before this owner task can become done, even
                # when cancellation bypasses the per-request Exception path.
                self._owned_poisoned = True
                issued_ticket['transport'].poisoned = True
            if self._ready and not self._ready.done():
                self._ready.set_exception(
                    exc if isinstance(exc, Exception) else ConnectionError(f"Memery worker stopped: {exc!r}")
                )
            raise
        finally:
            self._bounded_write_reserved = False
            if maintenance is not None:
                await asyncio.gather(maintenance, return_exceptions=True)
            if active_future is not None and not active_future.done():
                active_future.set_exception(ConnectionError("Memery worker stopped during request"))
            # Fail anything still queued so no caller awaits forever.
            while requests and not requests.empty():
                pending = requests.get_nowait()
                if pending is not None:
                    _, _, fut = pending
                    queued_ticket=getattr(fut,'_owned_write_ticket',None)
                    if type(queued_ticket) is dict and queued_ticket.get('owned_original_payload') is not None and queued_ticket.get('issued') is False:
                        queued_ticket['may_issue']=False;queued_ticket['owned_io_drained']=True
                    if not fut.done():
                        fut.set_exception(ConnectionError("Memery worker connection closed"))

    async def shutdown(self, *, _reset_handoff_token=None) -> None:
        self._assert_reset_handoff_access(_reset_handoff_token)
        self._s08_policy_witness=None
        self._code_query_closing = True
        # A cancelled caller may still own a start RPC whose exact job token
        # has not arrived. Drain its one-shot cleanup before closing the owner;
        # do not restart a closed provider just to send cancellation.
        for start, binding in list(self.__dict__.get("_code_query_pending_starts", {}).items()):
            self._cancel_code_query_start(start, binding)
        cleanups = list(self.__dict__.get("_code_query_cleanups", ()))
        if cleanups:
            await asyncio.shield(asyncio.gather(*cleanups, return_exceptions=True))
        if self._broker_client is not None:
            await self._broker_client.aclose()
            self._broker_client = None
            self._broker_ready = False
        if self._owner_task:
            if self._requests is not None:
                await self._requests.put(None)
            try:
                await self._owner_task
            except Exception as exc:
                logger.warning("Memery shutdown: owner task ended with %r", exc)
            self._owner_task = None
            self._requests = None
            self._ready = None

    async def _broker_request(
        self, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        if self._broker_client is None:
            raise MemoryTransportError("Memery broker is not initialized")
        try:
            response = await self._broker_client.post(
                self._broker_url,
                headers={
                    INTERNAL_TOOL_HEADER: self._broker_token,
                    INTERNAL_TOOL_OWNER_HEADER: self._broker_owner,
                    INTERNAL_TOOL_WORKSPACE_HEADER: self._broker_workspace,
                    **({"X-OpenClank-Code-Binding": str(self._env.get("OPEN_CLANK_CODE_BINDING_TOKEN") or os.environ.get("OPEN_CLANK_CODE_BINDING_TOKEN") or "")} if name == "code_query" else {}),
                    "Content-Type": "application/json",
                },
                json={"name": name, "arguments": arguments},
            )
            if getattr(response, "status_code", 200) in {
                400,
                401,
                403,
                404,
                405,
                409,
                410,
                415,
                422,
                429,
            }:
                if name == "memory_forget" and response.status_code == 409:
                    try:
                        rejected = response.json()
                    except Exception:
                        rejected = None
                    detail = rejected.get("detail") if isinstance(rejected, dict) else None
                    if isinstance(detail, dict) and detail.get("code") == "code_node_forget_requires_workspace_erase":
                        from src.memory_provider import code_node_forget_refusal_detail
                        refusal = MemoryRequestRejectedError(code="code_node_forget_requires_workspace_erase")
                        public = code_node_forget_refusal_detail(refusal)
                        raise MemoryRequestRejectedError(public["message"], code=public["code"])
                raise MemoryRequestRejectedError(
                    f"Memery broker {name} rejected the request"
                )
            response.raise_for_status()
            payload = response.json()
        except MemoryRequestRejectedError:
            raise
        except Exception as exc:
            raise MemoryTransportError(
                f"Memery broker {name} failed: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise MemoryTransportError(
                f"Memery broker {name} returned a non-object response"
            )
        return payload

    async def invoke_tool(
        self, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Internal typed transport boundary shared by app, lifetools, and MiMo."""
        from src.memory_versioned import VersionedMemoryError
        from src.memery_v2 import V2OperationError
        try:
            return await self._invoke_scoped_tool(name, arguments)
        except (VersionedMemoryError, V2OperationError) as exc:
            # Admission and optimistic-concurrency rejections are definite,
            # not transport failures that might encourage a blind retry.
            raise MemoryRequestRejectedError(str(exc)) from exc

    async def _invoke_scoped_tool(
        self, name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        self._assert_reset_handoff_access()
        if name == _HOST_CANDIDATE_REVIEW:
            raise MemoryRequestRejectedError("host-private review is unavailable through generic dispatch")
        if name in {"handler_display_label", "mark_conversation_source_removed", "capture", "versioned_detail", "versioned_transition", "assign_trust", "get_trust", "resolve_question"}:
            # Python-owned current revisions/trust must execute at the broker's
            # pinned authority, never at an unpinned agent's default SQLite.
            from copy import copy
            scoped = copy(self)
            # The scope changes arguments only. Route every pipe call through
            # the live supervisor, including after a generation change.
            scoped._local_db_path = self._local_db_path
            scoped._host_worker = False
            scoped._call_tool_transport = self._call_tool_transport
            scoped._workspace_id = str(arguments.get("workspace_id") or self._workspace_id)
            owner = arguments.get("owner")
            if name == "handler_display_label":
                return {"label": await scoped.handler_display_label(owner=owner)}
            if name == "mark_conversation_source_removed":
                return await scoped.mark_conversation_source_removed(owner=owner, session_id=str(arguments.get("session_id") or ""))
            if name == "capture" and "content" in arguments and not ({"user_text", "assistant_text"} & arguments.keys()):
                # A manual capture uses content rather than a pair of turn messages.
                # Bind it to the host-selected scope before submitting it on the worker pipe.
                fields = ("content", "capture_mode", "role", "category", "source", "session_id")
                payload = {key: arguments[key] for key in fields if key in arguments}
                payload.update(owner=owner, workspace_id=scoped._workspace_id)
                if payload.get("session_id"):
                    payload["session_key"] = payload["session_id"]
                result = await scoped._call_tool("capture", payload)
                for record_id in result.get("record_ids", ()):
                    if str(record_id).startswith("m_"):
                        await scoped._mirror_admitted_record(record_id, owner, scoped._workspace_id)
                return result
            if name == "capture":
                return await scoped.capture(
                    str(arguments.get("user_text") or ""), str(arguments.get("assistant_text") or ""),
                    owner=owner, session_id=arguments.get("session_id"),
                    capture_mode=str(arguments.get("capture_mode") or "candidate"),
                    source=str(arguments.get("source") or "odysseus"),
                )
            if name == "versioned_detail":
                return await scoped.versioned_detail(str(arguments.get("memory_id") or ""), owner=owner)
            if name == "versioned_transition":
                changes = {key: arguments[key] for key in (
                    "value", "value_type", "expected_value_type", "kind", "tags",
                    "target_revision", "reason",
                ) if key in arguments}
                return await scoped.versioned_transition(
                    str(arguments.get("memory_id") or ""), str(arguments.get("action") or ""),
                    expected_revision=arguments["expected_revision"], owner=owner, **changes,
                )
            if name == "resolve_question":
                return {"resolved": await scoped.resolve_question(
                    str(arguments.get("memory_id") or ""), owner=owner,
                    resolved_by=arguments.get("resolved_by"), answer=arguments.get("answer"),
                    expected_revision=arguments.get("expected_revision"),
                )}
            trust_args = {key: arguments[key] for key in (
                "subject_kind", "subject_id", "subject_revision", "project_id",
            ) if key in arguments}
            trust_args.update(owner=owner, workspace_id=scoped._workspace_id)
            if name == "assign_trust":
                trust_args.update({key: arguments[key] for key in (
                    "trust", "state", "reason_code", "rationale", "evidence_ids", "expected_assignment_id",
                ) if key in arguments})
                return await scoped.assign_trust(**trust_args)
            return await scoped.get_trust(**trust_args)
        return await self._call_tool(name, arguments)

    async def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self._assert_reset_handoff_access()
        from src.openclank.attachment_admission import admit_references, settle_references
        admitted = []
        if name in {"capture", "update_memory", "versioned_transition", "review_candidate", "resolve_memory", "resolve_question"}:
            admitted = admit_references(arguments.get("owner") or self._env.get("FM_OWNER"), "memery", arguments)
        result = await self._call_tool_transport(name, arguments)
        settle_references(admitted)
        if name == "owner_lifecycle" and arguments.get("action") in {"purge", "reset_commit"} and self._local_db_path():
            from src.openclank.attachment_admission import refresh_domain_references
            from src.openclank.attachment_inventory import memery_inventory
            await asyncio.to_thread(refresh_domain_references, "memery", lambda: memery_inventory(self._local_db_path()))
        return result

    async def _call_tool_transport(self, name: str, arguments: Dict[str, Any], *, _ready_only=False) -> Dict[str, Any]:
        from src.openclank.fm_owned_stdio import WorkerRejected

        self._assert_reset_handoff_access()
        if self._owned_poisoned:
            raise MemoryTransportError('guarded memory transport generation is unavailable')
        if name == _HOST_CANDIDATE_REVIEW:
            raise MemoryRequestRejectedError("host-private review is unavailable through generic transport")
        if _ready_only and (self._broker_url or not self._owner_task or self._owner_task.done()
                            or not self._ready or not self._ready.done() or self._ready.cancelled()
                            or self._ready.exception() is not None):
            raise MemoryTransportError("Indexing scheduler owner is unavailable")
        if self._broker_url:
            if not self._broker_ready:
                await self.initialize()
            return await self._broker_request(name, arguments)
        if not self._owner_task or self._owner_task.done():
            await self.initialize()
        if self._owned_poisoned:
            raise MemoryTransportError('guarded memory transport generation is unavailable')
        self._assert_reset_handoff_access()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if name in {"code_query", "code_index"}:
            mark_code_request_queued(fut)
        try:
            self._requests.put_nowait((name, arguments, fut))
        except asyncio.QueueFull as exc:
            raise MemoryRequestRejectedError("Memery worker queue is full", code="busy") from exc
        try:
            result, _ = await fut
            return result
        except MemoryRequestRejectedError:
            raise
        except WorkerRejected as exc:
            raise MemoryRequestRejectedError(str(exc), code=exc.code) from exc
        except Exception as exc:
            raise MemoryTransportError(f"Memery {name} failed: {exc}") from exc

    async def code_index_scheduler(self, *, action, revision=None, max_threads=None, limits=None, ready_only=True):
        """Private app bridge. Settings readback cannot start/restart an owner."""
        from src.openclank.code_index_settings import integer, legacy_default_limits, MAX_REVISION, validate_status, validate_limits
        if self._broker_url:
            raise MemoryRequestRejectedError("Indexing scheduler is app-only")
        arguments = {"action": action, "origin": "app"}
        if action == "configure":
            revision = integer(revision, "revision", 0, MAX_REVISION)
            max_threads = integer(max_threads, "max_threads", 1, 32)
            limits = validate_limits(limits) if limits is not None else None
            if revision == 0 and not legacy_default_limits(max_threads, revision, limits):
                raise MemoryRequestRejectedError("Invalid indexing scheduler revision")
            arguments.update(revision=revision, max_threads=max_threads)
            if limits is not None:
                arguments["limits"] = limits
        elif action != "status" or revision is not None or max_threads is not None or limits is not None:
            raise MemoryRequestRejectedError("Invalid indexing scheduler action")
        return validate_status(await self._call_tool_transport("code_index_scheduler", arguments, _ready_only=ready_only))

    async def _restore_index_scheduler(self):
        from src.settings import load_settings
        from src.openclank.code_index_settings import configuration_request, configured, configured_limits
        if self._broker_url:
            raise MemoryRequestRejectedError("Indexing scheduler is app-only")
        lock = self.__dict__.setdefault("_index_scheduler_lock", asyncio.Lock())
        async with lock:
            # This is a normal index admission, not a Settings GET. Reconnection
            # remains demand-owned, with no automatic relaunch/watchdog.
            await self.initialize()
            saved = load_settings()
            status = await self.code_index_scheduler(**configuration_request(saved), ready_only=True)
            requested_limits = configured_limits(saved)
            if ((status["desired"], status["revision"]) != configured(saved)
                    or status["desired_limits"] != requested_limits
                    or status["effective_limits"] != requested_limits):
                raise MemoryTransportError("Indexing configuration is pending")

    async def code_index_lifecycle(
        self, *, action: str, owner: str, owner_subject_id: str,
        workspace_id: str, location_id: str, workspace_revision: int,
        policy_generation: int, root: Optional[str], subscriber_id: str, origin: str,
        removal_confirmation: Optional[str] = None, replace_stale_root: bool = False,
    ) -> Dict[str, Any]:
        """App-only operation; native admission queues work in this worker."""
        if self._broker_url:
            raise MemoryRequestRejectedError("Code lifecycle is app-only")
        self._scope(owner)
        if action not in {"ensure", "status", "release", "cancel", "remove", "admit", "preview_removals", "confirm_removals"}:
            raise MemoryRequestRejectedError("Unsupported code lifecycle action")
        if origin not in {"app", "agent"} or not all(
            (owner, owner_subject_id, workspace_id, location_id, subscriber_id)
        ) or (action not in {"release", "cancel", "remove"} and not root):
            raise MemoryRequestRejectedError("Code lifecycle requires a validated Workspace")
        if action in {"ensure", "admit", "confirm_removals"}:
            await self._restore_index_scheduler()
        return await self._call_tool("code_index", {
            "action": action, "owner": owner, "owner_subject_id": owner_subject_id,
            "workspace_id": workspace_id, "location_id": location_id,
            "workspace_revision": workspace_revision, "policy_generation": policy_generation,
            **({"root": root} if root is not None else {}),
            "subscriber_id": subscriber_id, "origin": origin,
            **({"removal_confirmation": removal_confirmation} if removal_confirmation is not None else {}),
            **({"replace_stale_root": True} if replace_stale_root else {}),
        })

    def _own_code_query_cleanup(self, operation):
        """Request cleanup ownership survives a second caller cancellation."""
        tasks = self.__dict__.setdefault("_code_query_cleanups", set())
        task = asyncio.create_task(operation)
        tasks.add(task)
        def finished(done):
            tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Retrieve errors even if both waiters left.
        task.add_done_callback(finished)
        return task

    async def _cancel_code_query_token(self, binding, token):
        if not self._owner_task or self._owner_task.done():
            return  # A closed native owner cannot retain a running job.
        try:
            await self._call_tool("code_query", {"action": "cancel", "binding": binding, "token": token})
        except Exception:
            logger.debug("Code query cancellation owner unavailable")

    def _cancel_code_query_start(self, start, binding):
        known = self.__dict__.setdefault("_code_query_start_cleanups", {})
        if start in known:
            return known[start]
        async def after_ack():
            try:
                ticket = await start
                token = ticket.get("token")
                if isinstance(token, str) and token:
                    await self._cancel_code_query_token(binding, token)
            except Exception:
                logger.debug("Code query start owner unavailable during cleanup")
        cleanup = self._own_code_query_cleanup(after_ack())
        known[start] = cleanup
        cleanup.add_done_callback(lambda done: known.pop(start, None))
        return cleanup

    @measured_code_query
    async def code_query(self, *, binding: Dict[str, Any], query: Optional[Dict[str, Any]], source_claim=None, _read_fence_only=False, _selection_policy_fence=False, _visual_request=None, _retrospective_request=None, _admitted_code_vectors=None, _code_vector_index=None, _scip_request=None) -> Dict[str, Any]:
        """Private app-owned read job. Polls never admit/rebuild a workspace.

        Each RPC returns promptly to the single owner queue; CPU/parse/source
        work is performed by the existing native process outside that queue.
        """
        self._assert_reset_handoff_access()
        task=asyncio.current_task()
        if task is None: raise MemoryRequestRejectedError('memory query owner unavailable')
        queries=self._reset_handoff_state['queries'];queries.add(task)
        task.add_done_callback(queries.discard)
        if self._broker_url:
            raise MemoryRequestRejectedError("Trusted code binding is app-only")
        if _retrospective_request is not None:
            if (binding.get("origin") != "app" or query is not None or source_claim is not None or _read_fence_only
                    or _selection_policy_fence or _visual_request is not None or type(_retrospective_request) is not dict
                    or len(json.dumps(_retrospective_request,ensure_ascii=True,separators=(',',':')).encode()) > 196*1024):
                raise MemoryRequestRejectedError("Invalid retrospective request", code="invalid")
        visual_mode = None
        if _visual_request is not None:
            if binding.get("origin") != "app":
                raise MemoryRequestRejectedError("Code visualization is app-only", code="denied")
            if (query is not None or source_claim is not None or _read_fence_only or _selection_policy_fence
                    or not isinstance(_visual_request, dict) or set(_visual_request) != {"mode", "request"}
                    or _visual_request.get("mode") not in ("map", "selection")
                    or not isinstance(_visual_request.get("request"), dict)):
                raise MemoryRequestRejectedError("Invalid visual request mode", code="invalid")
            visual_mode = _visual_request["mode"]
        if self.__dict__.get("_code_query_closing", False):
            return {"error": {"code": "cancelled", "message": "Code query owner closing"}, "cost": {"native_rpc_calls_completed": 0}}
        self._scope(binding.get("owner"))
        arguments = {"action": "start", "binding": dict(binding)}
        if _scip_request is not None:
            if (binding.get("origin") not in ("app", "agent") or query is not None or source_claim is not None
                    or _read_fence_only or _selection_policy_fence or _visual_request is not None
                    or _retrospective_request is not None or _admitted_code_vectors is not None
                    or _code_vector_index is not None or type(_scip_request) is not dict
                    or set(_scip_request) != {"receipt", "index_base64", "source"}
                    or len(json.dumps(_scip_request, separators=(",", ":")).encode()) > 716 * 1024):
                raise MemoryRequestRejectedError("Invalid private SCIP observation request", code="invalid")
            arguments["scip_request"] = _scip_request
        elif _code_vector_index is not None:
            if (binding.get("origin") != "app" or query is not None or source_claim is not None
                    or _read_fence_only or _selection_policy_fence or _visual_request is not None
                    or _retrospective_request is not None or _admitted_code_vectors is not None
                    or type(_code_vector_index) is not dict
                    or len(json.dumps(_code_vector_index, separators=(",", ":")).encode()) > 196*1024):
                raise MemoryRequestRejectedError("Invalid private Code vector index request", code="invalid")
            arguments["code_vector_index"] = copy.deepcopy(_code_vector_index)
        elif _retrospective_request is not None:
            arguments["retrospective_request"] = copy.deepcopy(_retrospective_request)
        elif visual_mode is not None:
            arguments["visual_request"] = copy.deepcopy(_visual_request)
        else:
            arguments["query"] = dict(query)
        if _admitted_code_vectors is not None:
            if (not isinstance(query, dict) or query.get("operation") != "search"
                    or "semantic" not in query.get("channels", []) or source_claim is not None
                    or _read_fence_only or _selection_policy_fence or _visual_request is not None
                    or _retrospective_request is not None or type(_admitted_code_vectors) is not dict
                    or len(json.dumps(_admitted_code_vectors, separators=(",", ":")).encode()) > 196*1024):
                raise MemoryRequestRejectedError("Invalid private Code vector input", code="invalid")
            arguments["admitted_code_vectors"] = _admitted_code_vectors
        if source_claim is not None:
            arguments["source_claim"] = source_claim
        if _selection_policy_fence and not _read_fence_only:
            raise MemoryRequestRejectedError("Selection policy fence requires read fence mode", code="invalid")
        if _read_fence_only:
            arguments["read_fence_only"] = True
        if _selection_policy_fence:
            arguments["selection_policy_fence"] = True
        start = asyncio.create_task(self._call_tool("code_query", arguments))
        pending = self.__dict__.setdefault("_code_query_pending_starts", {})
        pending[start] = dict(binding)
        try:
            with code_performance_span(CodeStage.START):
                ticket = await asyncio.shield(start)
        except asyncio.CancelledError:
            cleanup = self._cancel_code_query_start(start, dict(binding))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass  # Still provider-owned and drained by shutdown.
            raise
        finally:
            pending.pop(start, None)
        completed_rpcs = 1
        token = ticket.get("token")
        if not isinstance(token, str) or not token:
            raise MemoryTransportError("Invalid code query job receipt")
        if self.__dict__.get("_code_query_closing", False):
            # Shutdown owns the same pending start. Do not race its sentinel
            # with a poll or implicitly initialize a replacement native owner.
            await asyncio.shield(self._cancel_code_query_start(start, dict(binding)))
            return {"error": {"code": "cancelled", "message": "Code query owner closing"}, "cost": {"native_rpc_calls_completed": completed_rpcs}}
        observation_delay = 0.001
        try:
            while True:
                if self.__dict__.get("_code_query_closing", False):
                    await asyncio.shield(self._own_code_query_cleanup(self._cancel_code_query_token(dict(binding), token)))
                    return {"error": {"code": "cancelled", "message": "Code query owner closing"}, "cost": {"native_rpc_calls_completed": completed_rpcs}}
                with code_performance_span(CodeStage.POLL):
                    reply = await self._call_tool("code_query", {"action": "poll", "binding": binding, "token": token})
                completed_rpcs += 1
                state = reply.get("state")
                if state == "completed" and isinstance(reply.get("result"), dict):
                    result = reply["result"]
                    if visual_mode is not None:
                        if (set(result) != {"mode", "result"} or result.get("mode") != visual_mode
                                or not isinstance(result.get("result"), dict)
                                or not isinstance(result["result"].get("cost"), dict)):
                            raise MemoryTransportError("Invalid code visual job result")
                        result = result["result"]
                    result.setdefault("cost", {})["native_rpc_calls_completed"] = completed_rpcs
                    return result
                if state in {"failed", "cancelled"}:
                    return {"error": reply.get("error") or {"code": "unavailable", "message": "Code query unavailable"}, "cost": {"native_rpc_calls_completed": completed_rpcs}}
                if state != "running":
                    raise MemoryTransportError("Invalid code query job state")
                # This is asynchronous observation, not a job timeout/lease or
                # a worker restart mechanism. Total work duration is unknown.
                with code_performance_span(CodeStage.OBSERVATION):
                    await asyncio.sleep(observation_delay)
                # Keep short queries responsive, but pending reads need not
                # consume forty owner-queue RPCs per second indefinitely.
                # Sleep remains cancellable; this cap is not a job deadline.
                observation_delay = min(observation_delay * 2, 0.100)
        except asyncio.CancelledError:
            cleanup = self._own_code_query_cleanup(self._cancel_code_query_token(dict(binding), token))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass  # The one-shot cleanup is not cancelled by its waiter.
            raise

    async def _release_code_topology(self, binding, token):
        """Drain the original topology job before releasing its retained result."""
        while self._owner_task is not None and not self._owner_task.done():
            reply = await self._call_tool("code_query", {"action": "release", "binding": binding, "token": token})
            if (not isinstance(reply, dict) or set(reply) != {"state", "token"}
                    or reply.get("token") != token or reply.get("state") not in {"running", "released"}):
                raise MemoryTransportError("Invalid topology release receipt")
            if reply["state"] == "released":
                return
            await asyncio.sleep(0.05)

    async def code_topology(self, *, binding, request, current, deadline):
        """One app-only owned topology capture; no model or public source data."""
        from src.openclank.graph_topology_codec import PageCollector
        self._assert_reset_handoff_access()
        task = asyncio.current_task()
        if task is None or self._broker_url or not isinstance(binding, dict) or binding.get("origin") != "app":
            raise MemoryRequestRejectedError("Topology authority unavailable")
        if (type(request) is not dict or set(request) != {"version", "profile", "paths", "expected_receipt", "limits"}
            or type(request.get("version")) is not int or request["version"] != 1
            or request.get("profile") != "indexed_topology_v1" or type(request.get("paths")) is not list
            or type(request.get("limits")) is not dict or set(request["limits"]) != {"files", "nodes", "edges", "work", "bytes", "page_bytes"}
            or not callable(current) or current() is not True or type(deadline) not in (int, float)
            or not math.isfinite(deadline) or deadline <= asyncio.get_running_loop().time()):
            raise MemoryRequestRejectedError("Topology request unavailable")
        limits = request["limits"]
        if (any(type(limits[k]) is not int or limits[k] <= 0 for k in limits)
            or limits["bytes"] > 64 * 1024 * 1024 or limits["page_bytes"] > 128 * 1024):
            raise MemoryRequestRejectedError("Topology budget unavailable")
        self._scope(binding.get("owner"))
        if self.__dict__.get("_code_query_closing", False):
            raise MemoryRequestRejectedError("Topology owner closing")
        queries = self._reset_handoff_state["queries"]
        queries.add(task); task.add_done_callback(queries.discard)
        frozen_binding = copy.deepcopy(binding)
        collector = PageCollector(receipt=copy.deepcopy(request["expected_receipt"]),
            paths=request["paths"], max_bytes=limits["bytes"])
        start = asyncio.create_task(self._call_tool("code_query", {"action": "start", "binding": frozen_binding,
            "topology_request": copy.deepcopy(request)}))
        pending = self.__dict__.setdefault("_code_query_pending_starts", {})
        pending[start] = frozen_binding
        async def drain_start():
            try:
                result = await start
                token = result.get("token") if isinstance(result, dict) else None
                if isinstance(token, str) and token:
                    await self._release_code_topology(frozen_binding, token)
            except Exception:
                logger.debug("Topology start cleanup unavailable")
        try:
            try:
                ticket = await asyncio.shield(start)
            except asyncio.CancelledError:
                cleanup = self._own_code_query_cleanup(drain_start())
                try: await asyncio.shield(cleanup)
                except asyncio.CancelledError: pass
                raise
        finally:
            pending.pop(start, None)
        if (not isinstance(ticket, dict) or set(ticket) != {"state", "token"} or ticket.get("state") != "running"
            or not isinstance(ticket.get("token"), str) or not ticket["token"]):
            if isinstance(ticket, dict) and isinstance(ticket.get("token"), str) and ticket["token"]:
                cleanup = self._own_code_query_cleanup(self._release_code_topology(frozen_binding, ticket["token"]))
                try: await asyncio.shield(cleanup)
                except asyncio.CancelledError: pass
            raise MemoryTransportError("Invalid topology job receipt")
        token = ticket["token"]; delay = 0.001
        try:
            while True:
                if (self.__dict__.get("_code_query_closing", False) or current() is not True
                    or asyncio.get_running_loop().time() >= deadline):
                    raise MemoryRequestRejectedError("Topology authority or deadline changed")
                reply = await self._call_tool("code_query", {"action": "poll", "binding": frozen_binding,
                    "token": token, "topology_cursor": collector.next_cursor})
                if current() is not True or asyncio.get_running_loop().time() >= deadline:
                    raise MemoryRequestRejectedError("Topology authority or deadline changed")
                if not isinstance(reply, dict) or reply.get("token") != token:
                    raise MemoryTransportError("Invalid topology job response")
                state = reply.get("state")
                if state == "running":
                    await asyncio.sleep(delay); delay = min(delay * 2, 0.1); continue
                if state != "completed" or not isinstance(reply.get("result"), dict):
                    raise MemoryRequestRejectedError("Topology capture unavailable")
                collector.accept(reply["result"])
                if collector.terminal is not None:
                    raw, record = collector.finish()
                    if current() is not True:
                        raise MemoryRequestRejectedError("Topology authority changed")
                    return {"bytes": raw, "record": record, "capture_id": collector.capture_id,
                        "scope_sha256": collector.scope_sha256}
        finally:
            cleanup = self._own_code_query_cleanup(self._release_code_topology(frozen_binding, token))
            try: await asyncio.shield(cleanup)
            except asyncio.CancelledError: pass

    async def code_visual(self, *, binding, visual_request):
        """App map/selection uses the existing cancellable native read owner.

        Full request/result schema and final workspace validation belong to
        the trusted visual accessor and native producer, not an agent bridge.
        """
        if visual_request is None:
            raise MemoryRequestRejectedError("Visual request required", code="invalid")
        return await self.code_query(binding=binding, query=None, _visual_request=visual_request)

    async def code_read_fence(self, *, binding, expected_receipt=None, selection_policy=False, budgets=None):
        result = await self.code_query(binding=binding, query={"operation": "capabilities", "fields": [], "channels": [], "expected_receipt": expected_receipt, **({"budgets": budgets} if budgets is not None else {})}, _read_fence_only=True, _selection_policy_fence=selection_policy)
        if isinstance(result.get("private_read_fence"), str):
            return {"fence": result["private_read_fence"], "cost": result.get("cost", {})}
        return result

    async def code_query_session(self, *, query: Dict[str, Any], session_id: str, authority_workspace_id: str, _outline_request=None) -> Dict[str, Any]:
        """Friendly bridge lane: trusted process binding, never public roots."""
        if not self._broker_url:
            raise MemoryRequestRejectedError("Code session broker is unavailable")
        process_session = str(self._env.get("SESSION_ID") or os.environ.get("SESSION_ID") or "")
        process_workspace = str(self._env.get("OPEN_CLANK_AUTHORITY_WORKSPACE_ID") or os.environ.get("OPEN_CLANK_AUTHORITY_WORKSPACE_ID") or "")
        token = str(self._env.get("OPEN_CLANK_CODE_BINDING_TOKEN") or os.environ.get("OPEN_CLANK_CODE_BINDING_TOKEN") or "")
        revision = str(self._env.get("OPEN_CLANK_SESSION_BINDING_REVISION") or os.environ.get("OPEN_CLANK_SESSION_BINDING_REVISION") or "")
        if not token or not process_session or session_id != process_session or authority_workspace_id != process_workspace or not revision.isdigit():
            raise MemoryRequestRejectedError("Code chat binding is unavailable")
        return await self._broker_request("code_query", {"query": query, "session_id": process_session, "binding_revision": int(revision), **({"outline_request": _outline_request} if _outline_request is not None else {})})

    def _scope(self, owner: Optional[str], session_id: Optional[str] = None) -> MemoryScope:
        bound_owner = str(self._env.get("FM_OWNER") or "").strip()
        requested_owner = str(owner or "").strip()
        if bound_owner and requested_owner and requested_owner != bound_owner:
            raise MemoryRequestRejectedError(
                "request scope conflicts with authenticated process scope"
            )
        return MemoryScope(
            owner=memory_owner(bound_owner or requested_owner),
            workspace_id=self._workspace_id,
            workspace_path=self._workspace_id,
            session_id=session_id,
            session_key=session_id,
        )

    @staticmethod
    def _record(data: Dict[str, Any]) -> MemoryRecord:
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        updated_at = data.get("updated_at") or data.get("created_at")
        timestamp = 0
        if isinstance(updated_at, str):
            try:
                timestamp = int(datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        def _score(key: str) -> Optional[float]:
            value = data.get(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        wire_kind = str(data.get("kind") or "fact")
        is_open_question = wire_kind == "open_question"
        return MemoryRecord(
            id=data.get("id", ""),
            text=data.get("content", data.get("text", "")),
            timestamp=timestamp,
            category=metadata.get("category", "unknown" if is_open_question else data.get("kind", "episodic")),
            source=data.get("source", ""),
            owner=data.get("owner"),
            session_id=data.get("session_id"),
            metadata=metadata,
            pinned=bool(metadata.get("pinned", False) or data.get("source_label") == "pinned"),
            workspace_id=data.get("workspace_id"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            uses=int(metadata.get("uses", 0) or 0),
            kind="unknown" if is_open_question else wire_kind,
            # Memery is an external wire boundary. Missing provenance
            # must not silently turn an engine record into user-authored truth.
            source_type=str(data.get("source_type") or "auto_extracted"),
            priority=int(data["priority"]) if isinstance(data.get("priority"), (int, float)) else None,
            trust_score=_score("trust_score"),
            confidence_score=_score("confidence_score"),
            importance_score=_score("importance_score"),
            scene_name=data.get("scene_name"),
            tags=[str(t) for t in data.get("tags") or [] if isinstance(t, (str, int))],
            source_message_ids=[str(m) for m in data.get("source_message_ids") or [] if isinstance(m, (str, int))],
            workspace_path=data.get("workspace_path"),
            archived=bool(data.get("archived", False)),
            exempt_from_decay=bool(data.get("exempt_from_decay", False)),
            exempt_from_dedup=bool(data.get("exempt_from_dedup", False)),
            last_accessed_at=data.get("last_accessed_at"),
            source_uri=data.get("source_uri") or metadata.get("source_uri"),
            source_revision=data.get("source_revision", metadata.get("source_revision")),
            content_hash=data.get("content_hash") or metadata.get("content_hash"),
            recall_explanation=(
                dict(metadata.get("recall_explanation"))
                if isinstance(metadata.get("recall_explanation"), dict)
                else {}
            ),
            provenance_conflict=bool(
                data.get("provenance_conflict", metadata.get("provenance_conflict", False))
            ),
            trust=(
                dict(data.get("trust"))
                if isinstance(data.get("trust"), dict)
                else (
                    dict(metadata.get("trust"))
                    if isinstance(metadata.get("trust"), dict)
                    else None
                )
            ),
        )

    async def _legacy_overlay_map(
        self, ids: List[str], *, owner: Optional[str]
    ) -> Dict[str, MemoryRecord]:
        """Batch-read legacy overlay rows in one local sqlite query.

        The legacy wire contract still owns pinned/uses/archived counters;
        one worker ``get_memory`` round-trip per record made v2 list/recall
        reads O(n) on the transport. Returns {} when no local store is
        available (broker mode without a pinned FM_DB_PATH); callers then
        keep the per-record transport path.
        """
        local_db = self._local_db_path()
        wanted = [str(value) for value in dict.fromkeys(ids) if value]
        if local_db is None or not wanted:
            return {}
        scope = self._scope(owner)
        rows = await asyncio.to_thread(
            _legacy_overlay_rows, local_db, scope.owner, scope.workspace_id, wanted
        )
        return {str(row["id"]): self._record(row) for row in rows}

    async def _overlay_legacy_fields(
        self,
        record: Optional[MemoryRecord],
        *,
        owner: Optional[str],
        legacy_map: Optional[Dict[str, MemoryRecord]] = None,
    ) -> Optional[MemoryRecord]:
        """Overlay counters still owned by the legacy wire contract."""
        if record is None:
            return None
        if legacy_map is not None:
            legacy = legacy_map.get(str(record.id))
        elif self._local_db_path() is not None:
            legacy = (await self._legacy_overlay_map([record.id], owner=owner)).get(
                str(record.id)
            )
        else:
            legacy = await self._get_legacy_current(record.id, owner=owner)
        if legacy is None:
            return record
        record.pinned = legacy.pinned
        record.uses = legacy.uses
        record.last_accessed_at = legacy.last_accessed_at
        record.archived = legacy.archived
        record.metadata = {**record.metadata, **legacy.metadata}
        return record

    async def capture(
        self,
        user_text: str,
        assistant_text: str,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        source: str = "odysseus",
        capture_mode: str = "candidate",
    ) -> Dict[str, Any]:
        """Automatic per-turn capture through the candidates-tier pipeline.
        ``review_only`` stages eligible content without auto-admission. The
        engine derives a stable event id from the scope and texts, so retries
        deduplicate."""
        scope = self._scope(owner, session_id)
        args: Dict[str, Any] = {
            "user_text": user_text or "",
            "assistant_text": assistant_text or "",
            "capture_mode": capture_mode,
            "workspace_id": scope.workspace_id,
            "workspace_path": scope.workspace_path,
            "owner": scope.owner,
            "source": source,
        }
        if session_id:
            args["session_id"] = session_id
            args["session_key"] = session_id
        result = await self._call_tool("capture", args)
        # Keep the v2 job envelope in the same install.  The legacy engine
        # still owns admission during compatibility rollout, but automatic
        # capture must not disappear from the versioned pipeline.
        local_db = self._local_db_path()
        if local_db is None:
            return result
        try:
            from src.memery_v2 import mirror_capture_job
            mirror_capture_job(
                owner=scope.owner,
                session_id=session_id,
                user_text=user_text,
                assistant_text=assistant_text,
                workspace_id=scope.workspace_id,
                db_path=local_db,
            )
            # Auto-capture may admit one or more curated records.  Refresh the
            # exact legacy rows before mirroring so a preferred v2 read can
            # never lag a successful turn capture.
            for record_id in result.get("record_ids") or []:
                if str(record_id).startswith("m_"):
                    await self._mirror_admitted_record(record_id, scope.owner, scope.workspace_id)
        except Exception:
            logger.debug("v2 capture mirror unavailable", exc_info=True)
        return result

    async def _mirror_admitted_record(self, record_id, owner, workspace_id):
        """Project the exact native record, including filtering and source provenance."""
        record = await self._get_legacy_current(record_id, owner=owner, workspace_id=workspace_id)
        if record is None:
            raise MemoryTransportError("Memery admitted a record that cannot be read in its scope")
        local_db = self._local_db_path()
        if local_db is not None:
            from src.memery_v2 import mirror_record
            await asyncio.to_thread(mirror_record, record, owner=owner, workspace_id=workspace_id, db_path=local_db)
        return record

    async def remember(
        self,
        text: str,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        category: str = "fact",
        source: str = "user",
        metadata: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[str] = None,
        capture_mode: str = "manual",
        source_type: Optional[str] = None,
    ) -> MemoryRecord:
        scope = self._scope(owner, session_id)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=workspace_id or scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        resolved_source_type = str(
            source_type or _SOURCE_TYPE_MAP.get(source, "auto_extracted")
        ).strip().lower()
        if resolved_source_type not in {
            "human",
            "ai",
            "auto_extracted",
            "procedural",
        }:
            resolved_source_type = "auto_extracted"
        args: Dict[str, Any] = {
            "content": text,
            "capture_mode": capture_mode,
            "workspace_id": workspace_id or scope.workspace_id,
            "workspace_path": scope.workspace_path,
            "owner": scope.owner,
            "source": source,
            "category": category,
            "source_type": resolved_source_type,
        }
        if session_id:
            args["session_id"] = session_id
            args["session_key"] = session_id
        if metadata:
            args["metadata"] = metadata

        result = await self._call_tool("capture", args)
        record_ids = result.get("record_ids") or []
        if not record_ids:
            raise RuntimeError("Memery capture returned no durable record id")
        if capture_mode == "review_only":
            record_id = next(
                (rid for rid in record_ids if str(rid).startswith("candidate_")),
                None,
            )
        else:
            record_id = next(
                (rid for rid in record_ids if str(rid).startswith("m_")),
                None,
            )
        if not record_id:
            raise RuntimeError(
                "Memery capture did not admit the requested memory"
            )
        if capture_mode != "review_only":
            return await self._mirror_admitted_record(record_id, scope.owner, workspace_id or scope.workspace_id)
        record_metadata = {**(metadata or {}), "pending_review": True}
        return MemoryRecord(
            id=record_id,
            text=text,
            category=category,
            kind=category,
            source=source,
            source_type=resolved_source_type,
            owner=scope.owner,
            session_id=session_id,
            metadata=record_metadata,
            pinned=bool(record_metadata.get("pinned", False)),
            workspace_id=workspace_id or scope.workspace_id,
        )

    async def _rank_fused_recall(self, hits, *, owner, workspace, deadline=None):
        """Apply the accepted account policy once, after recall fusion."""
        if not hits or len(hits)>100 or (deadline is not None and asyncio.get_running_loop().time()>=deadline):
            return hits
        ids=[str(hit.memory.id) for hit in hits]
        if len(set(ids))!=len(ids) or any(not value for value in ids):
            return hits
        proof=None
        try:
            loop=asyncio.get_running_loop()
            limit=min(loop.time()+0.2,deadline) if deadline is not None else loop.time()+0.2
            if not self._native_memory_ops_ready():
                def enabled_policy():
                    from src.openclank.memory_policy_projection import policy_coordinator
                    from src.openclank.memory_policy import KEY, policy_view
                    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
                    coordinator=policy_coordinator()
                    if coordinator is None or coordinator.resolve(owner)[1]!=workspace:return False
                    with PREFERENCE_LOCK:
                        accepted=policy_view(_load_for_user(owner).get(KEY))['accepted']
                    rank={} if accepted is None else accepted['policy'].get('rank_learning',{})
                    return rank.get('enabled') is True and any(rank.get(k) is True for k in ('salience','age_decay','retrieval_boost'))
                remaining=limit-loop.time()
                if remaining<=0 or not await asyncio.wait_for(asyncio.to_thread(enabled_policy),timeout=remaining):return hits
                def prepare_rank():
                    from src.openclank.memory_policy_projection import policy_coordinator
                    coordinator=policy_coordinator()
                    if coordinator is None:return None
                    account,target=coordinator.resolve(owner)
                    if target!=workspace:return None
                    return coordinator._rank_cold_preparation(owner,account,target,self)
                remaining=limit-loop.time()
                if remaining<=0:return hits
                proof=await asyncio.wait_for(asyncio.to_thread(prepare_rank),timeout=remaining)
                if proof is None or not await self._host_negotiate_native_memory_ops(owner=owner,workspace_id=workspace,deadline=limit,_rank_proof=proof):return hits
            if NATIVE_RANK_LEARNING_OPERATION not in self._native_memory_ops_support():return hits
            def capture():
                from src.openclank.memory_policy_projection import policy_coordinator
                coordinator=policy_coordinator()
                if coordinator is None:return None
                account,target=coordinator.resolve(owner)
                if target!=workspace:return None
                scope=coordinator._rank_read_context(owner,account,target,self,_proof=proof)
                return None if scope is None else (account,target,scope)
            remaining=limit-loop.time()
            if remaining<=0: return hits
            captured=await asyncio.wait_for(asyncio.to_thread(capture),timeout=remaining)
            if captured is None: return hits
            account,target,scope=captured
            refs,raw,recheck,close=scope
            try:
                body={'version':1,'owner':owner,'workspace_id':workspace,'account_id':account,
                      'policy_projection':refs,'policy_input_json':raw,'candidate_ids':ids}
                if loop.time()>=limit:return hits
                result=await self._s07_owner_rpc(owner,workspace,{'phase':'m3_rank','request':body},limit,read_ticket={})
                if result is None:
                    logger.debug('M3 rank unavailable; retaining baseline order')
                    return hits
                remaining=limit-loop.time()
                if remaining<=0:return hits
                if await asyncio.wait_for(asyncio.to_thread(recheck),timeout=remaining) is not True:return hits
            finally:close()
            if (type(result) is not dict or set(result)!={'version','state','ordered_ids'}
                    or type(result.get('version')) is not int or result['version']!=1
                    or result.get('state') not in ('ranked','off') or type(result.get('ordered_ids')) is not list
                    or len(result['ordered_ids'])!=len(ids) or set(result['ordered_ids'])!=set(ids)
                    or (result['state']=='off' and result['ordered_ids']!=ids)):
                return hits
            by_id=dict(zip(ids,hits))
            return [by_id[value] for value in result['ordered_ids']]
        except Exception:
            # Optional rank learning never turns unavailable custody into an
            # authorization or disrupts the baseline recall.
            return hits
        finally:
            if proof is not None:proof.close()

    async def _rank_s07_selected(self, selection, identities, *, owner, workspace, deadline):
        """Rank paired, already-selected references without changing FTS evidence."""
        from types import SimpleNamespace
        from src.openclank.memory_context_admission import Selection
        hits=[SimpleNamespace(memory=SimpleNamespace(id=identities[(c.revision.block_id,c.revision.revision,c.revision.value_sha256)]),candidate=c) for c in selection.candidates]
        ranked=await self._rank_fused_recall(hits,owner=owner,workspace=workspace,deadline=deadline)
        return Selection(selection.policy,tuple(hit.candidate for hit in ranked))

    async def recall(
        self,
        query: str,
        *,
        owner: Optional[str] = None,
        top_k: int = 5,
    ) -> List[MemorySearchHit]:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        v2_read_mode = os.environ.get("FM_V2_READ_MODE", "preferred").strip().lower()
        if v2_read_mode in {"preferred", "strict"} and local_db is not None:
            from src.memery_v2 import search_current_records
            rows = await asyncio.to_thread(
                search_current_records,
                query,
                owner=scope.owner,
                workspace_id=scope.workspace_id,
                top_k=max(1, int(top_k)) * (2 if v2_read_mode == "preferred" else 1),
                require_legacy_presence=True,
                db_path=local_db,
                )
            if rows or v2_read_mode == "strict":
                # Preferred mode is a shadow-read rollout, not a lossy
                # replacement: records written before the v2 mirror (or by
                # an older process) must remain recallable.  The v2 row wins
                # for an ID it owns; legacy-only rows fill the remainder.
                hits = []
                by_id: dict[str, MemorySearchHit] = {}
                legacy_map = (
                    await self._legacy_overlay_map(
                        [str(row.get("id")) for row in rows], owner=owner
                    )
                    if local_db is not None
                    else None
                )
                for row in rows:
                    returned_text = row.get("text")
                    record = await self._overlay_legacy_fields(
                        self._record(row), owner=owner, legacy_map=legacy_map
                    )
                    hit = MemorySearchHit(
                        memory=record,
                        provider_id=self.provider_id,
                        score=row.get("recall_score"),
                        revision_ref=(row.get("_revision_ref")
                                      if record is not None and record.text == returned_text
                                      else None),
                    )
                    hits.append(hit)
                    by_id[str(record.id)] = hit
                if v2_read_mode == "strict":
                    return await self._rank_fused_recall(hits, owner=scope.owner, workspace=scope.workspace_id)
                merged_top_k = max(1, int(top_k))
                legacy_result = await self._call_tool("recall", {
                    "query": query,
                    "top_k": merged_top_k * 2,
                    "tier": "curated",
                    "workspace_id": scope.workspace_id,
                    "owner": scope.owner,
                })
                ranks = {str(hit.memory.id): 1.0 / (60 + rank)
                         for rank, hit in enumerate(hits, 1)}
                seen_legacy: set[str] = set()
                for rank, mem in enumerate(legacy_result.get("memories", []), 1):
                    record = self._record(mem)
                    record_id = str(record.id)
                    if record_id in seen_legacy:
                        continue
                    seen_legacy.add(record_id)
                    ranks[record_id] = ranks.get(record_id, 0.0) + 1.0 / (60 + rank)
                    if record_id not in by_id:
                        hit = MemorySearchHit(memory=record, provider_id=self.provider_id)
                        hits.append(hit)
                        by_id[record_id] = hit
                for hit in hits:
                    hit.score = ranks[str(hit.memory.id)]
                hits.sort(key=lambda hit: (-hit.score, str(hit.memory.id)))
                hits = await self._rank_fused_recall(hits, owner=scope.owner, workspace=scope.workspace_id)
                return hits[:merged_top_k]
        args: Dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "tier": "curated",
            "workspace_id": scope.workspace_id,
            "owner": scope.owner,
        }
        result = await self._call_tool("recall", args)
        hits: List[MemorySearchHit] = []
        for mem in result.get("memories", []):
            record = self._record(mem)
            hits.append(MemorySearchHit(
                memory=record,
                provider_id=self.provider_id,
                score=mem.get("score"),
            ))
        return await self._rank_fused_recall(hits, owner=scope.owner, workspace=scope.workspace_id)

    async def list_memories(
        self,
        *,
        owner: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryRecord]:
        records: List[MemoryRecord] = []
        cursor: Optional[str] = None
        while len(records) < limit:
            page, cursor = await self.list_page(
                owner=owner,
                limit=min(1000, limit - len(records)),
                cursor=cursor,
            )
            records.extend(page)
            if cursor is None:
                break
        return records

    async def list_page(
        self,
        *,
        owner: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> tuple[List[MemoryRecord], Optional[str]]:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        # Transitional cutover: v2 is preferred only when explicitly enabled,
        # and rows must still have a live legacy source until all writers are
        # retired. This keeps stale shadow/test rows out of user reads while
        # letting production traffic exercise the canonical projection.
        v2_read_mode = os.environ.get("FM_V2_READ_MODE", "preferred").strip().lower()
        if v2_read_mode in {"preferred", "strict"} and local_db is not None:
            v2_records = await self.list_v2_records(
                owner=owner,
                limit=limit,
                require_legacy_presence=True,
            )
            if v2_read_mode == "strict":
                return v2_records, None
            # Do not hide legacy-only records while the mirror is still
            # catching up.  The legacy cursor remains authoritative for page
            # boundaries; v2 rows replace matching IDs in that page.
            legacy_result = await self._call_tool(
                "list_memories",
                {
                    "owner": scope.owner,
                    "workspace_id": scope.workspace_id,
                    "limit": limit,
                    "cursor": cursor,
                },
            )
            legacy_records = [
                self._record(record)
                for record in legacy_result.get("records", [])
            ]
            preferred = {str(record.id): record for record in v2_records}
            merged = [preferred.get(str(record.id), record) for record in legacy_records]
            if cursor is None:
                # A resolved question archives its compatibility row by
                # design, so the legacy-cursor-driven merge would drop it.
                # Surface resolved answers once, on the first page.
                merged_ids = {str(record.id) for record in merged}
                for record in v2_records:
                    if (
                        str(record.id) not in merged_ids
                        and getattr(record, "kind", "") == "unknown"
                        and record.metadata.get("v2_status") == "active"
                    ):
                        merged.append(record)
            return merged, legacy_result.get("next_cursor")
        result = await self._call_tool(
            "list_memories",
            {
                "owner": scope.owner,
                "workspace_id": scope.workspace_id,
                "limit": limit,
                "cursor": cursor,
            },
        )
        return [self._record(record) for record in result.get("records", [])], result.get("next_cursor")

    async def list_v2_records(
        self,
        *,
        owner: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 1000,
        require_legacy_presence: bool = False,
    ) -> List[MemoryRecord]:
        """Read the typed v2 projection for parity/cutover checks.

        This method is intentionally explicit while legacy worker reads remain
        the compatibility authority. It gives Brain/Agent integration tests a
        real v2-native path without silently changing existing user results.
        """
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            # Broker mode without a pinned FM_DB_PATH: the broker owns the
            # store and direct sqlite would read the wrong database.
            return []
        from src.memery_v2 import list_current_records
        rows = await asyncio.to_thread(
            list_current_records,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            project_id=project_id,
            limit=limit,
            require_legacy_presence=require_legacy_presence,
            db_path=local_db,
        )
        records = []
        legacy_map = await self._legacy_overlay_map(
            [str(row.get("id")) for row in rows], owner=owner
        )
        for row in rows:
            record = await self._overlay_legacy_fields(
                self._record(row), owner=owner, legacy_map=legacy_map
            )
            # list_page needs the canonical status to tell a resolved
            # question (active, legacy row archived by design) from a stale
            # open one; the wire kind alone cannot.
            record.metadata = {**record.metadata, "v2_status": row.get("status")}
            records.append(record)
        return records

    async def count_v2_records(
        self,
        *,
        owner: Optional[str] = None,
        project_id: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Exact number of v2 records `list_v2_records(require_legacy_presence=True)` (what the panel list reads) returns
        over all pages; None when there is no local store to read. Legacy-only rows not yet mirrored into v2 are not counted."""
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            return None
        from src.memery_v2 import count_current_records
        return await asyncio.to_thread(
            count_current_records,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            project_id=project_id,
            filters=filters,
            require_legacy_presence=True,
            db_path=local_db,
        )

    async def search_v2_with_receipt(
        self,
        query: str,
        *,
        owner: Optional[str] = None,
        top_k: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Optional[tuple]:
        """Lexical v2 search (the rows `search` reads) plus its component receipt; None without a local store."""
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            return None
        from src.memery_v2 import search_current_records
        return await asyncio.to_thread(
            search_current_records,
            query,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            top_k=top_k,
            require_legacy_presence=True,
            filters=filters,
            with_receipt=True,
            db_path=local_db,
        )

    async def entity_lookup_v2(
        self,
        *,
        owner: Optional[str] = None,
        alias: Optional[str] = None,
        entity_type: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Scoped alias / entity-type lookup (Native graph_walk op=entities); the owner comes from the scope, never the request."""
        scope = self._scope(owner)
        args: Dict[str, Any] = {"op": "entities", "owner": scope.owner, "workspace_id": scope.workspace_id, "limit": limit}
        if alias is not None:
            args["alias"] = alias
        if entity_type is not None:
            args["entity_type"] = entity_type
        result = await self._call_tool("graph_walk", args)
        if not (isinstance(result, dict) and isinstance(result.get("entities"), list) and isinstance(result.get("receipt"), dict)):
            raise MemoryRequestRejectedError("entity lookup unavailable")
        return {"entities": result["entities"], "receipt": result["receipt"]}

    # Related-memory budget: hop-1 neighbours expanded (at most), edges read per expansion. A cut is reported in the receipt.
    _RELATED_NEIGHBOUR_CAP = 10
    _RELATED_EDGE_LIMIT = 25

    async def related_memories(
        self,
        memory_id: str,
        *,
        owner: Optional[str] = None,
        mode: str = "graph",
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Records related to one record. ``graph``: typed edges of the Native graph, at most two hops (record -> shared
        node -> record), each item with the edges walked and one traversal receipt. Semantic lookup requires a
        published current curated-memory vector generation. Unknown or foreign graph ids return an empty result.
        None means there is no local store."""
        scope = self._scope(owner)
        if mode == "semantic":
            return {"mode": "semantic", "state": "unavailable", "reason": "curated_generation_unavailable", "items": []}
        if mode != "graph":
            raise MemoryRequestRejectedError("related mode unsupported")
        local_db = self._local_db_path()
        if local_db is None:
            return None
        base = {"owner": scope.owner, "workspace_id": scope.workspace_id}
        async def expand(**node):
            result = await self._call_tool("graph_walk", {"op": "expand", "limit": self._RELATED_EDGE_LIMIT, **base, **node})
            if not (isinstance(result, dict) and isinstance(result.get("hits"), list) and isinstance(result.get("receipt"), dict)):
                raise MemoryRequestRejectedError("related graph unavailable")
            return result
        first = await expand(node_kind="memory", node_name=memory_id)
        cut = [first["receipt"].get("truncated_by")]
        start = first.get("node_id")
        related: Dict[str, Dict[str, Any]] = {}
        def add(record_id, hops, steps):
            if record_id and record_id != memory_id and (record_id not in related or related[record_id]["hops"] > hops):
                related[record_id] = {"id": record_id, "hops": hops, "edges": steps}
        neighbours: Dict[str, Dict[str, Any]] = {}
        for hit in first["hits"]:
            edge, other = hit.get("edge") or {}, hit.get("other") or {}
            step = {"tag": edge.get("tag"), "direction": hit.get("direction"), "fact": edge.get("fact"), "trust": edge.get("trust"),
                    "via": {"kind": other.get("kind"), "name": other.get("name")}}
            if other.get("kind") == "memory":
                add(other.get("name"), 1, [step])
            elif other.get("id") and other.get("id") != start:
                neighbours.setdefault(other["id"], step)
        ordered = sorted(neighbours)
        if len(ordered) > self._RELATED_NEIGHBOUR_CAP:
            cut.append("nodes")
        for node_id in ordered[: self._RELATED_NEIGHBOUR_CAP]:
            second = await expand(node_id=node_id)
            cut.append(second["receipt"].get("truncated_by"))
            for hit in second["hits"]:
                edge, other = hit.get("edge") or {}, hit.get("other") or {}
                if other.get("kind") == "memory":
                    add(other.get("name"), 2, [neighbours[node_id], {"tag": edge.get("tag"), "direction": hit.get("direction"),
                                                                    "fact": edge.get("fact"), "trust": edge.get("trust")}])
        truncated_by = "nodes" if "nodes" in cut else ("edges" if "edges" in cut else None)
        receipt = {"complete": truncated_by is None, "limits": {"hops": 2, "nodes": self._RELATED_NEIGHBOUR_CAP, "edges": self._RELATED_EDGE_LIMIT},
                   "truncated_by": truncated_by}
        # Only records the owner can see now (lineage/withheld/archived rules of the list) and that pass the closed filters.
        from src.memery_v2 import list_current_records
        visible = await asyncio.to_thread(
            list_current_records, owner=scope.owner, workspace_id=scope.workspace_id, limit=None,
            require_legacy_presence=True, filters=filters, _raise_errors=True, db_path=local_db)
        items = []
        for row in sorted(visible, key=lambda r: (related.get(str(r.get("id")), {}).get("hops", 9), str(r.get("id")))):
            found = related.get(str(row.get("id")))
            if found:
                items.append({"id": row["id"], "text": row.get("text"), "kind": row.get("kind"), "status": row.get("status"),
                              "hops": found["hops"], "edges": found["edges"]})
        return {"mode": "graph", "state": "ok", "items": items[:limit], "receipt": receipt}

    async def inspect_tier(
        self,
        tier: str,
        *,
        owner: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        scope = self._scope(owner)
        if tier == "candidate":
            args = {
                "owner": scope.owner,
                "workspace_id": scope.workspace_id,
                "status": status,
                "limit": limit,
            }
            result = await self._call_tool("list_candidates", args)
            return list(result.get("candidates") or [])
        if tier == "quarantine":
            args = {
                "owner": scope.owner,
                "workspace_id": scope.workspace_id,
                "limit": limit,
            }
            result = await self._call_tool("list_quarantine", args)
            return list(result.get("quarantine") or [])
        if tier not in {"raw", "curated"}:
            raise ValueError("tier must be raw, candidate, curated, or quarantine")
        args: Dict[str, Any] = {
            "query": "",
            "tier": tier,
            "limit": limit,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        }
        result = await self._call_tool("search", args)
        return [dict(row.get("record", row)) for row in result.get("results", [])]

    async def _host_s09_digest(self, scope):
        """Optional account-bound read under the actual Host admission and process."""
        from core.models import get_session_manager_instance
        from src.openclank.account_request_barrier import AccountRequestBarrier
        manager=get_session_manager_instance();lifecycle=getattr(manager,'lifecycle_context',None) if manager else None
        auth=getattr(lifecycle,'auth_manager',None) if lifecycle else None
        barrier=getattr(lifecycle,'account_request_barrier',None) if lifecycle else None
        account_lifecycle=getattr(lifecycle,'account_owner_lifecycle',None) if lifecycle else None
        if (not lifecycle or getattr(lifecycle,'memory_provider',None) is not self or auth is None
                or account_lifecycle is None or type(barrier) is not AccountRequestBarrier):return None
        try:
            account=auth.account_id(scope.owner)
            if not account or auth.username_for_account_id(account)!=scope.owner:return None
        except Exception:return None
        transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task;preparation=self._guarded_preparation
        process=getattr(transport,'process',None)
        if transport is None or process is None or task is None or task.done() or preparation is None:return None
        try:admission=await barrier.acquire(scope.owner)
        except Exception:return None
        try:
            lease=_S09DigestLease(self,manager,lifecycle,auth,account_lifecycle,barrier,admission,scope.owner,scope.workspace_id,account,transport,generation,process,task,preparation)
            if not lease.current():return None
            await asyncio.to_thread(preparation.assert_valid)
            if not lease.current():return None
            ticket={};request={'version':1,'owner':scope.owner,'workspace_id':scope.workspace_id,'account_id':account}
            result=await self._s07_owner_rpc(scope.owner,scope.workspace_id,{'phase':'question_digest','request':request},
                asyncio.get_running_loop().time()+1.5,read_ticket=ticket,_s09_digest_lease=lease)
            await asyncio.to_thread(preparation.assert_valid)
            if (not lease.current() or ticket.get('transport') is not transport or ticket.get('generation') is not generation
                    or ticket.get('issued') is not True or ticket.get('frame_seen') is not True or ticket.get('frame_invalid') is not False
                    or ticket.get('terminal_error') is True or type(result) is not dict
                    or result.get('question_eligibility') not in ('available','unavailable')):return None
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        finally:
            await barrier.release(admission)

    async def digest(self, *, owner: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Index-card digest of the bank for the provider scope: counts,
        pinned headlines, top clusters, newest topics. Cheap and read-only —
        meant for per-turn injection, with recall as the deep-dive path.

        Display text is identity-rendered (%USER% → the Handler label) so the
        injected card and the Brain preview never show the literal token;
        stored claims are untouched, and any rendered field carries a
        ``raw_*`` companion for edit round-trips."""
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        result = await self._host_s09_digest(scope)
        if result is None:
            result = await self._call_tool(
                "digest",
                {"owner": scope.owner, "workspace_id": scope.workspace_id},
            )
        if not (isinstance(result, dict) and "counts" in result):
            return None
        try:
            label = await self.handler_display_label(owner=scope.owner)
            _render_digest_identity(result, label)
        except Exception:
            logger.debug("digest identity rendering unavailable", exc_info=True)
        return result

    async def reconstruct_graph(self, *, owner: str, request: Dict[str, Any]) -> Dict[str, Any]:
        scope = self._scope(owner)
        result = await self._call_tool("graph_walk", {
            "op": "reconstruct", "owner": scope.owner, "workspace_id": scope.workspace_id,
            "include_global": False, "reconstruction": request,
        })
        self._validate_graph_reconstruction(result, request)
        return result

    @staticmethod
    def _validate_graph_reconstruction(result, request):
        def reject():
            raise MemoryTransportError("Invalid graph reconstruction result")

        def identifier(value):
            return isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable()

        def witness(value):
            return (isinstance(value, dict) and identifier(value.get("edge_id"))
                    and identifier(value.get("from")) and identifier(value.get("to"))
                    and type(value.get("tag")) is str and len(value["tag"].encode()) <= 128
                    and (value.get("fact_id") is None or identifier(value["fact_id"])))

        if (not isinstance(result, dict) or result.get("version") != 1
                or type(result.get("stop_reason")) is not str
                or not isinstance(result.get("omitted"), list) or len(result["omitted"]) > 24
                or any(type(code) is not str or len(code) > 64 for code in result["omitted"])
                or type(result.get("scanned_edges")) is not int
                or not 0 <= result["scanned_edges"] <= request["max_edges"]
                or not isinstance(result.get("steps"), list) or len(result["steps"]) > request["max_steps"]
                or not isinstance(result.get("nodes"), list) or len(result["nodes"]) > request["max_nodes"]
                or not isinstance(result.get("reverse_cues"), list)
                or len(result["reverse_cues"]) > request["max_nodes"] * 4):
            reject()
        from math import isfinite
        for node in result["nodes"]:
            if (not isinstance(node, dict) or not identifier(node.get("id"))
                    or type(node.get("kind")) is not str or len(node["kind"].encode()) > 128
                    or type(node.get("score")) not in (int, float) or not isfinite(node["score"])
                    or not isinstance(node.get("lanes"), list) or len(node["lanes"]) > 4
                    or any(not isinstance(lane, dict) or not identifier(lane.get("lane"))
                           or type(lane.get("rank")) is not int or lane["rank"] < 1
                           for lane in node["lanes"])
                    or ("witness" in node and not witness(node["witness"]))):
                reject()
        for step in result["steps"]:
            if (not isinstance(step, dict) or type(step.get("step")) is not int
                    or not 1 <= step["step"] <= request["max_steps"]
                    or any(not isinstance(step.get(key), list) or len(step[key]) > request["max_nodes"]
                           or not all(identifier(value) for value in step[key])
                           for key in ("frontier", "discovered"))
                    or any(key in step and (type(step[key]) is not int or not 0 <= step[key] <= limit)
                           for key, limit in (("new_witnesses", request["max_edges"]),
                                              ("new_intent_witnesses", request["max_edges"]),
                                              ("new_evidence_kinds", request["max_nodes"])))):
                reject()
        ranked = {node["id"] for node in result["nodes"]}
        if any(not isinstance(row, dict) or not identifier(row.get("node_id"))
               or row["node_id"] not in ranked or type(row.get("cue")) is not str
               or len(row["cue"].encode()) > 256 for row in result["reverse_cues"]):
            reject()
        community = result.get("communities")
        if community is None:
            if request.get("community") == "broad":
                reject()
            return
        if (not isinstance(community, dict) or community.get("mode") != request.get("community", "focal")
                or community.get("mode") == "off"
                or community.get("algorithm") != "admitted_witness_label_propagation_v1"
                or type(community.get("converged")) is not bool or type(community.get("iterations")) is not int
                or not 0 <= community["iterations"] <= 12
                or not isinstance(community.get("groups"), list) or len(community["groups"]) > 6):
            reject()
        seen = set()
        witnesses = 0
        for group in community["groups"]:
            if (not isinstance(group, dict) or type(group.get("id")) is not str
                    or not re.fullmatch(r"[a-f0-9]{64}", group["id"])
                    or not isinstance(group.get("members"), list) or not group["members"]
                    or len(group["members"]) > request["max_nodes"]
                    or not all(identifier(value) for value in group["members"])
                    or not isinstance(group.get("anchor_ids"), list)
                    or len(group["anchor_ids"]) > len(group["members"])
                    or not all(identifier(value) for value in group["anchor_ids"])
                    or not isinstance(group.get("witnesses"), list) or not group["witnesses"]
                    or len(group["witnesses"]) > request["max_edges"]):
                reject()
            members = set(group["members"])
            if (seen & members or not set(group["anchor_ids"]) <= members
                    or any(not witness(edge) or edge["from"] not in members or edge["to"] not in members
                           for edge in group["witnesses"])):
                reject()
            seen.update(members)
            witnesses += len(group["witnesses"])
        if len(seen | ranked) > request["max_nodes"] or witnesses > result["scanned_edges"]:
            reject()

    async def graph(
        self,
        op: str,
        *,
        owner: Optional[str] = None,
        query: Optional[str] = None,
        node_id: Optional[str] = None,
        to_node_id: Optional[str] = None,
        tag: Optional[str] = None,
        direction: Optional[str] = None,
        limit: int = 25,
    ) -> Dict[str, Any]:
        """Read v2 knowledge plus explicit Memery entity relationships."""
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is not None:
            try:
                from services.memory.principal_context import ensure_principal_context_cached
                await asyncio.to_thread(
                    ensure_principal_context_cached,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    db_path=local_db,
                )
            except Exception:
                logger.debug("v2 principal bootstrap unavailable", exc_info=True)
        canonical: Dict[str, Any] = {}
        graph_preparation = self._guarded_preparation
        if local_db is not None:
            try:
                from src.memory_versioned import canonical_graph

                canonical = await asyncio.to_thread(
                    canonical_graph,
                    op,
                    owner=scope.owner,
                    workspace_id=scope.workspace_id,
                    query=query,
                    node_id=node_id,
                    to_node_id=to_node_id,
                    tag=tag,
                    direction=direction,
                    limit=limit,
                    db_path=local_db,
                    guarded_preparation=graph_preparation,
                )
                if self._guarded_preparation is not graph_preparation:
                    canonical = {}
            except Exception:
                logger.debug("canonical v2 graph unavailable; using Rust graph", exc_info=True)
        args: Dict[str, Any] = {
            "op": op,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
            "limit": limit,
        }
        if query is not None:
            args["query"] = query
        if node_id is not None:
            args["node_id"] = node_id
        if to_node_id is not None:
            args["dst_id"] = to_node_id
        if tag is not None:
            args["tag"] = tag
        if direction is not None:
            args["direction"] = direction
        has_canonical = {
            "overview": bool(canonical.get("node_total")),
            "cues": bool(canonical.get("hits")),
            "rank": bool(canonical.get("hits")),
            "tags": bool(canonical.get("tags")),
            "expand": bool(canonical.get("hits")),
            "fetch": bool(canonical.get("node")),
            "trace": bool(canonical.get("paths")),
        }.get(op, False)
        try:
            legacy = await self._call_tool("graph_walk", args)
        except Exception:
            if has_canonical and self._guarded_preparation is graph_preparation:
                logger.debug(
                    "explicit Memery graph unavailable; using canonical v2 graph",
                    exc_info=True,
                )
                return canonical
            raise
        if not has_canonical or self._guarded_preparation is not graph_preparation:
            return legacy

        def merged_rows(key: str) -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            seen = set()
            for row in [*(canonical.get(key) or []), *(legacy.get(key) or [])]:
                marker = str(row.get("id") or json.dumps(row, sort_keys=True, default=str))
                if marker in seen:
                    continue
                seen.add(marker)
                rows.append(row)
            return rows

        if op == "overview":
            nodes = merged_rows("nodes")
            edges = merged_rows("edges")
            visible_nodes = nodes[:limit]
            visible_node_ids = {str(row.get("id")) for row in visible_nodes}
            visible_edges = [
                edge
                for edge in edges
                if str(edge.get("src_id")) in visible_node_ids
                and str(edge.get("dst_id")) in visible_node_ids
            ][: max(limit * 3, limit)]
            return {
                **legacy,
                **canonical,
                "nodes": visible_nodes,
                "edges": visible_edges,
                "node_total": max(
                    len(nodes),
                    int(canonical.get("node_total") or 0)
                    + int(legacy.get("node_total") or 0),
                ),
                "edge_total": max(
                    len(edges),
                    int(canonical.get("edge_total") or 0)
                    + int(legacy.get("edge_total") or 0),
                ),
                "canonical": True,
                "explicit_relationships": True,
            }
        if op == "tags":
            counts: Dict[str, int] = {}
            for row in [*(canonical.get("tags") or []), *(legacy.get("tags") or [])]:
                name = str(row.get("tag") or "")
                if name:
                    counts[name] = counts.get(name, 0) + int(row.get("count") or 0)
            return {
                **legacy,
                **canonical,
                "tags": [
                    {"tag": name, "count": count}
                    for name, count in sorted(
                        counts.items(), key=lambda item: (-item[1], item[0])
                    )
                ][:limit],
                "canonical": True,
                "explicit_relationships": True,
            }
        if op == "fetch":
            return {
                **legacy,
                **canonical,
                "node": canonical.get("node") or legacy.get("node"),
                "canonical": True,
                "explicit_relationships": True,
            }
        key = "paths" if op == "trace" else "hits"
        merged = merged_rows(key)
        result = {
            **legacy,
            **canonical,
            key: merged[:limit],
            "canonical": True,
            "explicit_relationships": True,
        }
        receipt = legacy.get("receipt")
        if isinstance(receipt, dict) and len(merged) > limit and receipt.get("complete") is True:
            # The canonical + Native merge itself cut the rows: say so (the Native receipt only knows its own limits).
            result["receipt"] = {**receipt, "complete": False,
                                 "truncated_by": "edges" if op == "expand" else "nodes"}
        return result

    async def memory_quality(self, *, rebuild_graph_fts: bool = False) -> Dict[str, Any]:
        scope = self._scope(None)
        return await self._call_tool("memory_quality", {"rebuild_graph_fts": rebuild_graph_fts,
                                                        "owner": scope.owner, "workspace_id": scope.workspace_id})

    def _host_bounded_write_available(self) -> bool:
        """Read-only proof of the current initialized owned transport lifetime."""
        from src.openclank.fm_owned_stdio import OwnedStdio
        transport=self._owned_transport
        return bool(self._guarded_v17 and not self._broker_url and not self._owned_poisoned
            and type(transport) is OwnedStdio and transport.current(self._owned_generation)
            and self._owner_task is not None and not self._owner_task.done() and self._requests is not None
            and self._ready is not None and self._ready.done() and not self._ready.cancelled()
            and self._ready.exception() is None)

    async def _host_document_import_ready(self):
        """Factually negotiate this exact prepared database and live owned child."""
        from src.memery_database import GuardedStorePreparation
        from src.openclank.mimo_supervisor import _queued_owned_thread
        prepared=self._guarded_preparation; transport=self._owned_transport
        generation=self._owned_generation; task=self._owner_task
        if (type(prepared) is not GuardedStorePreparation or prepared.capabilities.get('document_import_v1') is not True
                or prepared.capabilities.get('document_fts') is not True or not self._host_bounded_write_available()):
            return False
        process=transport.process
        try: await _queued_owned_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error): return False
        return bool(self._guarded_preparation is prepared and self._owned_transport is transport
            and self._owned_generation is generation and self._owner_task is task
            and self._host_bounded_write_available() and transport.process is process)

    def _assert_reset_retirement_ready(self, expected=None):
        witness=self._reset_retirement_witness
        if ((expected is not None and witness is not expected) or type(witness) is not tuple or len(witness)!=5 or not self._host_bounded_write_available()
            or witness[0] is not self._owned_transport or witness[1] is not self._owned_generation
            or witness[2] is not self._owned_transport.process or witness[3] is not self._owner_task
            or witness[4] is not self._guarded_preparation):
            raise MemoryRequestRejectedError('queued reset live capability unavailable')
        witness[4].assert_valid()
        if (not self._host_bounded_write_available() or witness[0] is not self._owned_transport
            or witness[1] is not self._owned_generation or witness[2] is not self._owned_transport.process
            or witness[3] is not self._owner_task or witness[4] is not self._guarded_preparation):
            raise MemoryRequestRejectedError('queued reset live capability changed')
        return True

    def _reset_retirement_ready_local(self):
        witness=self._reset_retirement_witness
        return bool(type(witness) is tuple and len(witness)==5 and self._host_bounded_write_available()
            and witness[0] is self._owned_transport and witness[1] is self._owned_generation
            and witness[2] is self._owned_transport.process and witness[3] is self._owner_task
            and witness[4] is self._guarded_preparation)

    async def _host_negotiate_reset_retirement(self, *, owner, workspace_id):
        """Bind the versioned native feature to this exact owned child."""
        if self._reset_retirement_ready_local():
            try:
                await asyncio.to_thread(self._assert_reset_retirement_ready)
                return self._reset_retirement_ready_local()
            except Exception:
                return False
        self._reset_retirement_witness=None
        if not self._host_bounded_write_available(): return False
        transport=self._owned_transport;generation=self._owned_generation;process=transport.process
        task=self._owner_task;prepared=self._guarded_preparation
        try:
            await asyncio.to_thread(prepared.assert_valid)
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'reset_retirement_capability'},
                asyncio.get_running_loop().time()+2.0)
            expected={'version':1,'feature':'queued-reset-single-holder-v1','database_id':prepared.database_id,
                'device':prepared.device,'inode':prepared.inode}
            if (type(result) is not dict or result!=expected or type(result.get('version')) is not int
                or type(result.get('device')) is not int or type(result.get('inode')) is not int
                or not self._host_bounded_write_available() or self._owned_transport is not transport
                or self._owned_generation is not generation or transport.process is not process
                or self._owner_task is not task or self._guarded_preparation is not prepared): return False
            await asyncio.to_thread(prepared.assert_valid)
            self._reset_retirement_witness=(transport,generation,process,task,prepared)
            return self._reset_retirement_ready_local()
        except Exception:
            return False

    def _terminal_s08_ready_local(self):
        witness=self._terminal_s08_witness
        return bool(type(witness) is tuple and len(witness)==2 and witness[0] is self._reset_retirement_witness
            and self._reset_retirement_ready_local())

    def _assert_terminal_s08_ready(self, expected):
        if expected is None or self._terminal_s08_witness is not expected or not self._terminal_s08_ready_local():
            raise MemoryRequestRejectedError('terminal S08 reset capability unavailable')
        self._assert_reset_retirement_ready(expected[0])
        if self._terminal_s08_witness is not expected or not self._terminal_s08_ready_local():
            raise MemoryRequestRejectedError('terminal S08 reset capability changed')
        return True

    async def _host_negotiate_terminal_s08(self, *, owner, workspace_id):
        if self._terminal_s08_ready_local(): return True
        self._terminal_s08_witness=None
        if not self._reset_retirement_ready_local(): return False
        base=self._reset_retirement_witness;prepared=self._guarded_preparation
        try:
            await asyncio.to_thread(self._assert_reset_retirement_ready,base)
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'reset_terminal_s08_capability'},
                asyncio.get_running_loop().time()+2.0)
            expected={'version':1,'feature':'nonqueued-memories-terminal-s08-v1','database_id':prepared.database_id,
                'device':prepared.device,'inode':prepared.inode}
            if (type(result) is not dict or result!=expected or type(result.get('version')) is not int
                or type(result.get('device')) is not int or type(result.get('inode')) is not int
                or self._reset_retirement_witness is not base or not self._reset_retirement_ready_local()): return False
            await asyncio.to_thread(self._assert_reset_retirement_ready,base)
            if self._reset_retirement_witness is not base: return False
            self._terminal_s08_witness=(base,object())
            return self._terminal_s08_ready_local()
        except Exception:return False

    async def _settle_owned_read_ticket(self, owned, ticket):
        """Keep this owner-loop reservation until exact terminal or child wait."""
        terminal=(ticket.get('frame_seen') is True and not ticket.get('frame_invalid')) or ticket.get('positive_unsent') is True
        if not terminal:
            late=asyncio.create_task(ticket['terminal_event'].wait())
            child=asyncio.create_task(owned.process.wait())
            try:
                drain=asyncio.create_task(asyncio.wait((late,child),timeout=0.5,return_when=asyncio.FIRST_COMPLETED))
                while not drain.done():
                    try:await asyncio.shield(drain)
                    except asyncio.CancelledError:continue
                drain.result();exited=False
                if child.done() and not child.cancelled():
                    try:owned._confirm_exit(owned.process,child.result());exited=True
                    except Exception:pass
                terminal=(ticket.get('frame_seen') is True and not ticket.get('frame_invalid')) or exited
            finally:
                for pending in (late,child):
                    if not pending.done():pending.cancel()
                for pending in (late,child):
                    try:await pending
                    except (asyncio.CancelledError,Exception):pass
        if not terminal:
            self._owned_poisoned=True;owned.poisoned=True
            stop=asyncio.create_task(owned.terminate_and_wait(ticket['generation']))
            while not stop.done():
                try:await asyncio.shield(stop)
                except asyncio.CancelledError:continue
            try:stop.result()
            except BaseException:
                confirmation=owned.exit_confirmation(ticket['generation'])
                while not confirmation.done():
                    try:await asyncio.shield(confirmation)
                    except asyncio.CancelledError:continue
                confirmation.result()
        owned.disarm_read(ticket)
        if not ticket['settled'].done():ticket['settled'].set_result(None)

    async def _settle_bounded_write(self, fut, owner_task, ticket):
        """Drain an issued write through response or exact waited child death."""
        abort=asyncio.Event()
        async def stop_uncertain():
            self._owned_poisoned=True
            ticket['transport'].poisoned=True
            # Wake an owner already waiting on an empty queue so it can unwind
            # its own pipe after the child has stopped.
            if self._requests is not None:
                if self._requests.empty():
                    self._requests.put_nowait(None)
            stop=asyncio.create_task(ticket['transport'].terminate_and_wait(ticket['generation']))
            while not stop.done():
                try: await asyncio.shield(stop)
                except asyncio.CancelledError: pass
            try:
                stop.result()
            except BaseException:
                # Owner cleanup may still positively wait this exact child.
                # Keep the guard and surface a content-free diagnostic until
                # that same generation's wait proof becomes available.
                logger.warning('guarded memory child stop is unresolved; source write remains unsettled')
                confirmation=ticket['transport'].exit_confirmation(ticket['generation'])
                while not confirmation.done():
                    try: await asyncio.shield(confirmation)
                    except asyncio.CancelledError: pass
                confirmation.result()
            raise MemoryRequestRejectedError('bounded memory write outcome unknown')
        async def settle():
            aborted=asyncio.create_task(abort.wait())
            try:
                timeout=max(0.0,ticket['deadline']-asyncio.get_running_loop().time())
                done,_=await asyncio.wait((fut,owner_task,aborted),timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
                if fut in done and not fut.cancelled():
                    try:
                        return fut.result()
                    except Exception:
                        if ticket.get('terminal_error') is True and not ticket.get('frame_invalid'):
                            raise MemoryRequestRejectedError('bounded memory write rejected') from None
                        if not ticket['issued']:
                            ticket['may_issue']=False
                            raise MemoryRequestRejectedError('bounded memory write unavailable') from None
                if not ticket['issued']:
                    ticket['may_issue']=False
                    if not fut.done(): fut.cancel()
                    raise MemoryRequestRejectedError('bounded memory write unavailable')
                # EOF, a client exception, timeout and owner exit do not prove
                # that an already-issued native write has stopped.
                await stop_uncertain()
            except asyncio.CancelledError:
                if ticket['issued']:
                    await stop_uncertain()
                ticket['may_issue']=False
                if not fut.done(): fut.cancel()
                raise MemoryRequestRejectedError('bounded memory write unavailable') from None
            finally:
                aborted.cancel()
                try: await aborted
                except asyncio.CancelledError: pass
        task=asyncio.create_task(settle())
        cancelled=False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled=True
                abort.set()
            except Exception:
                break
        try:
            result=task.result()
        except BaseException:
            if cancelled: raise asyncio.CancelledError
            raise
        if cancelled: raise asyncio.CancelledError
        return result

    async def _host_candidate_request(self, owner: str, workspace_id: str, payload: Dict[str, Any], *, require_live: bool = False, write_deadline: Optional[float] = None, write_status=None, reset_feature=False, terminal_feature=None, before_issue=None, _reset_handoff_token=None, _reset_original=None, _s05_permit=None) -> Dict[str, Any]:
        self._assert_reset_handoff_access(_reset_handoff_token)
        s05_queue_before_issue=None
        if payload.get('phase') in _S05_RESERVE_WRITES:
            from src.openclank.memory_job_admission import _S05ReserveIssue
            if (type(_s05_permit) is not _S05ReserveIssue or not require_live or type(write_deadline) is not float
                    or getattr(before_issue,'__self__',None) is not _s05_permit
                    or getattr(before_issue,'__func__',None) is not _S05ReserveIssue.current):
                raise MemoryRequestRejectedError('S05 account admission unavailable')
            try: _s05_permit.assert_scope(self,owner,workspace_id,payload.get('phase'),payload.get('request'))
            except Exception as exc: raise MemoryRequestRejectedError('S05 account admission unavailable') from exc
            def s05_queue_before_issue():
                try:
                    return _s05_permit.assert_scope(self,owner,workspace_id,payload.get('phase'),payload.get('request')) is True
                except Exception:
                    return False
            def before_issue():
                try:
                    return _s05_permit.assert_current(self,owner,workspace_id,payload.get('phase'),payload.get('request')) is True
                except Exception:
                    return False
        elif _s05_permit is not None:
            raise MemoryRequestRejectedError('S05 account admission unavailable')
        source_phases = {'source_project','source_fence','source_reset_fence','source_renew','source_candidate',
            'paired_source_candidate','paired_source_fence','paired_rename_fence','paired_rename_renew',
            'pending_pair_fence','terminal_pair_fence','erased_pair_fence','pending_pair_rebind','phase_root_fence','phase_root_renew',
            'phase_root_observation_fence','phase_root_observation_renew','observation_renew'}
        nested = payload.get('request') if type(payload.get('request')) is dict else payload
        source_mapping = nested.get('source') if payload.get('phase') in source_phases else None
        if payload.get('phase') in source_phases and _provider_source_mapping_error(self, source_mapping):
            raise MemoryRequestRejectedError('host source profile unavailable')
        if source_mapping is not None and before_issue is not None:
            prior_before_issue = before_issue
            def before_issue():
                return not _provider_source_mapping_error(self, source_mapping) and prior_before_issue() is True
        if _reset_handoff_token is not None and not ((write_deadline is not None and payload.get('phase') == 'guarded_reset_commit') or (write_deadline is None and payload.get('phase') == 'guarded_reset_stamp')):
            raise MemoryRequestRejectedError('original reset issue unavailable')
        if payload.get('phase')=='guarded_reset_commit' and _reset_original is None and _reset_handoff_token is None:
            raise MemoryRequestRejectedError('original reset custody unavailable')
        if before_issue is not None and (payload.get('phase') not in ('mirror_apply','document_vector_selected_commit','document_vector_intent_prepare','document_vector_intent_claim','document_vector_intent_cancel','document_vector_intent_begin','document_vector_intent_append','document_vector_intent_finalize','document_vector_batch_begin','document_vector_batch_claim','document_vector_batch_complete','document_vector_batch_finalize','document_vector_begin','document_vector_append','document_vector_finalize','document_remove','document_upsert','document_structure_write','guarded_reset_cleanup','s05_reserve_prepare','s05_reserve','skill_draft_retire','skill_draft_prepare','skill_draft_reserve','skill_draft_dispatch','skill_draft_terminal','skill_draft_revoke','skill_draft_prepared_cancel','s08_model_prepare','s08_model_reserve','s08_model_dispatch','s08_policy_commit','s08_policy_terminal','s08_policy_prepared_cancel','s08_policy_review_apply','s02_importance_prepare','s02_importance_apply','s08_rules_once','paired_review_apply','explicit_add','explicit_add_review_edit','explicit_add_review_apply','import_review_apply','import_staged_review_edit','import_staged_review_apply','confirmed_question_apply','published_lifecycle_apply','owner_transition_cache_cleanup','owner_purge_cache_cleanup','paired_rename_fence','paired_rename_renew','source_candidate','paired_source_candidate','pending_pair_fence','terminal_pair_fence','erased_pair_fence','pending_pair_rebind','phase_root_fence','phase_root_renew','phase_root_observation_fence','phase_root_observation_renew','native_memory_ops_batch_apply','preextracted_stage','preextracted_host_anchor_stage','preextracted_review','knowledge_update_batch_apply','guarded_batch_forget','guarded_batch_forget_restore') or not callable(before_issue) or write_deadline is None):
            raise MemoryRequestRejectedError('paired review authority unavailable')
        if payload.get('phase') in ('mirror_apply','document_vector_selected_commit','document_vector_intent_prepare','document_vector_intent_claim','document_vector_intent_cancel','document_vector_intent_begin','document_vector_intent_append','document_vector_intent_finalize','document_vector_batch_begin','document_vector_batch_claim','document_vector_batch_complete','document_vector_batch_finalize','document_vector_begin','document_vector_append','document_vector_finalize','document_remove','document_upsert','document_structure_write','guarded_reset_cleanup','s05_reserve_prepare','s05_reserve','skill_draft_retire','skill_draft_prepare','skill_draft_reserve','skill_draft_dispatch','skill_draft_terminal','skill_draft_revoke','skill_draft_prepared_cancel','s08_model_prepare','s08_model_reserve','s08_model_dispatch','s08_policy_commit','s08_policy_terminal','s08_policy_prepared_cancel','s08_policy_review_apply','s02_importance_prepare','s02_importance_apply','s08_rules_once','paired_review_apply','explicit_add','explicit_add_review_edit','explicit_add_review_apply','import_review_apply','import_staged_review_edit','import_staged_review_apply','confirmed_question_apply','published_lifecycle_apply','owner_transition_cache_cleanup','owner_purge_cache_cleanup','paired_rename_fence','paired_rename_renew','source_candidate','paired_source_candidate','pending_pair_fence','terminal_pair_fence','erased_pair_fence','pending_pair_rebind','phase_root_fence','phase_root_renew','phase_root_observation_fence','phase_root_observation_renew','native_memory_ops_batch_apply','preextracted_stage','preextracted_host_anchor_stage','preextracted_review','knowledge_update_batch_apply','guarded_batch_forget','guarded_batch_forget_restore') and before_issue is None:
            raise MemoryRequestRejectedError('paired review authority unavailable')
        mode=payload.get('reset',{}).get('mode') if type(payload.get('reset')) is dict else None
        if mode is not None and (mode!='terminal-s08-v1' or terminal_feature is None):
            raise MemoryRequestRejectedError('terminal S08 reset capability unavailable')
        if terminal_feature is not None:
            from src.openclank.mimo_supervisor import _queued_owned_thread
            await _queued_owned_thread(self._assert_terminal_s08_ready,terminal_feature)
        if reset_feature:
            from src.openclank.mimo_supervisor import _queued_owned_thread
            await _queued_owned_thread(self._assert_reset_retirement_ready,None if reset_feature is True else reset_feature)
        if self._owned_poisoned:
            raise MemoryRequestRejectedError('host-private source transport unavailable')
        if not self._guarded_v17 or self._broker_url or self._local_db_path() is None or workspace_id != self._workspace_id:
            raise MemoryRequestRejectedError("host-private review is unavailable")
        if require_live and (not self._owner_task or self._owner_task.done() or not self._ready or not self._ready.done()
                             or self._ready.cancelled() or self._ready.exception() is not None):
            raise MemoryRequestRejectedError('host-private source transport unavailable')
        if not require_live and (not self._owner_task or self._owner_task.done()):
            await self.initialize()
        if self._owned_poisoned:
            raise MemoryRequestRejectedError('host-private source transport unavailable')
        if reset_feature and (not self._reset_retirement_ready_local() or (reset_feature is not True and self._reset_retirement_witness is not reset_feature)):
            raise MemoryRequestRejectedError('queued reset live capability changed')
        if terminal_feature is not None and (self._terminal_s08_witness is not terminal_feature or not self._terminal_s08_ready_local()):
            raise MemoryRequestRejectedError('terminal S08 reset capability changed')
        owner_task = self._owner_task
        fut = asyncio.get_running_loop().create_future()
        if payload.get("phase") in {"owner_transition_apply", "owner_transition_status", "owner_transition_stamp", "owner_purge_apply", "owner_purge_status", "owner_purge_stamp", "owner_transition_cache_cleanup", "owner_purge_cache_cleanup"}:
            nested = payload.get('request')
            if set(payload) != {'phase', 'request'} or type(nested) is not dict or nested.get('owner') != owner:
                raise MemoryRequestRejectedError('host-private owner scope changed')
            arguments = payload
        elif payload.get("phase") == "question_seed_status":
            nested=payload.get('request')
            original=nested.get('request') if type(nested) is dict else None
            if (set(payload)!={'phase','request'} or type(nested) is not dict or set(nested)!={'request','current_policy'}
                or type(original) is not dict or original.get('owner')!=owner or original.get('workspace_id')!=workspace_id):
                raise MemoryRequestRejectedError('host-private question status scope changed')
            arguments=payload
        elif payload.get('phase') in ('phase_root_observation_fence','phase_root_observation_renew'):
            nested=payload.get('request'); proof=nested.get('phase' if payload['phase']=='phase_root_observation_fence' else 'transition') if type(nested) is dict else None
            field='owner' if payload['phase']=='phase_root_observation_fence' else 'new_owner'
            if set(payload)!={'phase','request'} or type(proof) is not dict or proof.get(field)!=owner or nested.get('workspace_id')!=workspace_id:
                raise MemoryRequestRejectedError('phase observation scope changed')
            arguments=payload
        elif payload.get('phase')=='phase_root_renew':
            nested=payload.get('request'); transition=nested.get('transition') if type(nested) is dict else None
            if set(payload)!={'phase','request'} or type(transition) is not dict or transition.get('new_owner')!=owner or nested.get('workspace_id')!=workspace_id:
                raise MemoryRequestRejectedError('phase root scope changed')
            arguments=payload
        elif payload.get('phase')=='phase_root_fence':
            nested=payload.get('request'); phase=nested.get('phase') if type(nested) is dict else None
            if set(payload)!={'phase','request'} or type(phase) is not dict or phase.get('owner')!=owner or nested.get('workspace_id')!=workspace_id:
                raise MemoryRequestRejectedError('phase root scope changed')
            arguments=payload
        elif payload.get('phase') in ('pending_pair_fence','terminal_pair_fence','erased_pair_fence','pending_pair_rebind','pending_pair_original'):
            nested=payload.get('request')
            actual=(nested.get('phase') or {}).get('owner') if type(nested) is dict and payload['phase'] in ('pending_pair_fence','terminal_pair_fence','erased_pair_fence') else (nested.get('owner') if type(nested) is dict else None)
            if set(payload)!={'phase','request'} or type(nested) is not dict or actual!=owner or nested.get('workspace_id')!=workspace_id:
                raise MemoryRequestRejectedError('pending pair scope changed')
            arguments=payload
        elif payload.get('phase') in ('paired_rename_fence','paired_rename_renew'):
            nested=payload.get('request'); phase=nested.get('phase' if payload['phase']=='paired_rename_fence' else 'transition') if type(nested) is dict else None
            field='owner' if payload['phase']=='paired_rename_fence' else 'new_owner'
            if (set(payload)!={'phase','request'} or type(phase) is not dict or phase.get(field)!=owner
                    or nested.get('workspace_id')!=workspace_id):
                raise MemoryRequestRejectedError('paired rename scope changed')
            arguments=payload
        elif payload.get("phase") == "paired_reset_fence":
            nested=payload.get('request')
            if (set(payload)!={'phase','request','reset'} or type(nested) is not dict or nested.get('owner')!=owner or nested.get('workspace_id')!=workspace_id or type(payload.get('reset')) is not dict):
                raise MemoryRequestRejectedError('paired reset scope changed')
            arguments=payload
        elif payload.get('phase') in ('explicit_add_review_edit','explicit_add_review_apply','import_staged_review_edit','import_staged_review_apply','native_memory_ops_batch_apply'):
            nested=payload.get('request'); selector=nested.get('selector') if type(nested) is dict else None
            if (set(payload)!={'phase','request'} or type(selector) is not dict
                    or selector.get('owner')!=owner or selector.get('workspace_id')!=workspace_id):
                raise MemoryRequestRejectedError('explicit Add review scope changed')
            arguments=payload
        elif payload.get("phase") in {"guarded_batch_forget", "guarded_batch_forget_restore", "found_observation", "admitted_observation", "context_admitted_observation", "observation_renew", "source_renew", "source_candidate", "paired_source_candidate", "paired_source_fence", "paired_review_apply", "paired_review_status", "explicit_add", "explicit_add_status", "import_review_apply", "import_review_status", "derived_compaction", "standing_guidance_apply", "confirmed_question_apply", "published_lifecycle_apply", "question_seed_bootstrap", "account_candidate_apply", "account_value_successor_apply", "analysis_snapshot_publish", "domain_definition_review", "metadata_assignment_apply", "helped_observation", "preextracted_stage", "preextracted_host_anchor_stage", "preextracted_host_anchor_status", "preextracted_review", "knowledge_update_batch_apply"}:
            nested = payload.get("request")
            if (set(payload) != {"phase", "request"} or type(nested) is not dict
                    or nested.get("owner") != owner or nested.get("workspace_id") != workspace_id):
                raise MemoryRequestRejectedError("host-private operation scope changed")
            arguments = payload
        elif payload.get('phase') in _S05_RESERVE_WRITES:
            try: arguments = _s05_reserve_envelope(payload,owner,workspace_id)
            except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
                raise MemoryRequestRejectedError('S05 original unavailable') from exc
        elif payload.get('phase') in _SKILL_DRAFT_WRITES:
            try: arguments = _skill_draft_envelope(payload,owner,workspace_id)
            except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
                raise MemoryRequestRejectedError('skill draft original unavailable') from exc
        else:
            arguments = {"owner":owner, "workspace_id":workspace_id, **payload}
        if payload.get('phase') == 'helped_observation' and (write_deadline is None or not self._host_helped_ready()):
            raise MemoryRequestRejectedError('helped write unavailable')
        if write_deadline is not None:
            from src.openclank.fm_owned_stdio import OwnedStdio
            loop=asyncio.get_running_loop(); transport=self._owned_transport; generation=self._owned_generation
            if (payload.get('phase') not in ('mirror_apply','document_vector_selected_commit','document_vector_intent_prepare','document_vector_intent_claim','document_vector_intent_cancel','document_vector_intent_begin','document_vector_intent_append','document_vector_intent_finalize','document_vector_batch_begin','document_vector_batch_claim','document_vector_batch_complete','document_vector_batch_finalize','document_vector_begin','document_vector_append','document_vector_finalize','document_remove','document_upsert','document_structure_write','question_seed_bootstrap','account_candidate_apply','account_value_successor_apply','analysis_snapshot_publish','domain_definition_review','metadata_assignment_apply','s05_reserve_prepare','s05_reserve','skill_draft_retire','skill_draft_prepare','skill_draft_reserve','skill_draft_dispatch','skill_draft_terminal','skill_draft_revoke','skill_draft_prepared_cancel','s08_model_prepare','s08_model_reserve','s08_model_dispatch','s08_policy_commit','s08_policy_terminal','s08_policy_prepared_cancel','s08_policy_review_apply','s02_importance_prepare','s02_importance_apply','s08_rules_once','queued_origin_fence','source_reset_fence','observation_reset_fence','guarded_reset_commit','guarded_reset_cleanup','helped_observation','derived_compaction','standing_guidance_apply','paired_review_apply','paired_reset_fence','explicit_add','explicit_add_review_edit','explicit_add_review_apply','import_review_apply','import_staged_review_edit','import_staged_review_apply','confirmed_question_apply','published_lifecycle_apply','owner_transition_cache_cleanup','owner_purge_cache_cleanup','paired_rename_fence','paired_rename_renew','source_candidate','paired_source_candidate','pending_pair_fence','terminal_pair_fence','erased_pair_fence','pending_pair_rebind','phase_root_fence','phase_root_renew','phase_root_observation_fence','phase_root_observation_renew','native_memory_ops_batch_apply','preextracted_stage','preextracted_host_anchor_stage','preextracted_review','knowledge_update_batch_apply','guarded_batch_forget','guarded_batch_forget_restore') or not require_live or type(write_deadline) is not float
                or not math.isfinite(write_deadline) or loop.time()>=write_deadline or type(transport) is not OwnedStdio
                or not transport.current(generation) or self._owned_poisoned or self._owner_rpc_busy
                or self._s07_rpc_reserved or self._bounded_write_reserved or self._requests is None or not self._requests.empty()):
                raise MemoryRequestRejectedError('bounded memory write unavailable')
            if payload.get('phase') == 'derived_compaction':
                try:
                    OwnedStdio._argument_digest(arguments)
                except (TypeError, ValueError, UnicodeError, OverflowError) as exc:
                    raise MemoryRequestRejectedError('bounded memory write unavailable') from exc
            ticket={'deadline':write_deadline,'transport':transport,'generation':generation,'issued':False,'may_issue':True,'before_issue':before_issue}
            if s05_queue_before_issue is not None: ticket['queue_before_issue']=s05_queue_before_issue
            if write_status is not None: write_status['ticket']=ticket
            if payload.get('phase') in ('document_remove','document_upsert','document_structure_write','s05_reserve_prepare','s05_reserve','skill_draft_retire','skill_draft_prepare','skill_draft_reserve','skill_draft_dispatch','skill_draft_terminal','skill_draft_revoke','skill_draft_prepared_cancel','s08_model_prepare','s08_model_reserve','s08_model_dispatch','s08_policy_commit','s08_policy_terminal','s08_policy_prepared_cancel','connection_review_apply','s08_policy_review_apply','s02_importance_prepare','s02_importance_apply'):
                prepared=self._guarded_preparation
                if type(write_status) is not dict or prepared is None or transport.process is None:
                    raise MemoryRequestRejectedError('owned original unavailable')
                try:
                    frozen=json.loads(json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')))
                    frame_digest=OwnedStdio._argument_digest(arguments, document_write=payload.get('phase') in ('document_upsert','document_structure_write','document_vector_append','document_vector_intent_append'))
                except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
                    raise MemoryRequestRejectedError('owned original unavailable') from exc
                ticket.update(owned_original_payload=frozen,owned_original_frame_digest=frame_digest,
                    owned_original_database=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode),
                    owned_original_process=transport.process,owned_original_owner_task=owner_task,owned_io_drained=False)
            if _reset_original is not None:
                if (payload.get('phase')!='guarded_reset_commit' or _reset_handoff_token is not None
                    or type(_reset_original) is not tuple or len(_reset_original)!=6 or transport.process is None):
                    raise MemoryRequestRejectedError('original reset custody unavailable')
                reset_claim,reset_frozen,reset_issue_current,reservation,reset_barrier,reset_witness=_reset_original
                from services.memory.nuke_coordinator import MemoryResetClaim
                from src.openclank.account_request_barrier import AccountRequestBarrier, _ResetDrainWitness
                if (type(reset_claim) is not MemoryResetClaim or type(reset_barrier) is not AccountRequestBarrier
                    or type(reset_witness) is not _ResetDrainWitness or reset_witness.barrier is not reset_barrier
                    or reset_witness.owner!=reset_barrier._owner(reset_claim.owner)
                    or getattr(reset_claim._coordinator._reset_state,'account_request_barrier',None) is not reset_barrier
                    or reset_barrier.reset_witness_current(reset_witness) is not True):
                    raise MemoryRequestRejectedError('original reset custody unavailable')
                original=self._reset_handoff_state['original']
                reissued=type(reservation) is _OriginalResetUnsent
                if reissued:
                    self._assert_nonselective_reset_unsent(reservation,reset_claim,reset_frozen)
                elif (type(original) is not dict or original.get('reset_reservation') is not reservation
                    or original.get('reset_never_reserved') is not True or original.get('reset_claim') is not reset_claim
                    or original.get('transport') is not transport or original.get('generation') is not generation
                    or original.get('reset_process') is not transport.process or original.get('reset_owner_task') is not owner_task):
                    raise MemoryRequestRejectedError('original reset custody unavailable')
                if not callable(reset_issue_current) or reset_issue_current() is not True:
                    raise MemoryRequestRejectedError('original reset authority unavailable')
                frozen_copy=json.loads(json.dumps(reset_frozen,ensure_ascii=False,allow_nan=False))
                if type(frozen_copy) is not dict or (not reissued and original.get('reset_frozen')!=frozen_copy) or payload!={'phase':'guarded_reset_commit',**frozen_copy['request']}:
                    raise MemoryRequestRejectedError('original reset identity changed')
                if reset_claim.assert_current_post_dispatch(provider=self,frozen=frozen_copy) is not True:
                    raise MemoryRequestRejectedError('original reset custody unavailable')
                ticket.update(reset_claim=reset_claim,reset_frozen=frozen_copy,reset_process=transport.process,
                    reset_owner_task=owner_task,reset_future=fut,reset_io_settled=False,reset_unsent_drained=False,reset_unsent_fact=None,reset_reissued=reissued)
                self._reset_handoff_state['original']=ticket
            fut._owned_write_ticket=ticket
            def consume_late(done):
                if not done.cancelled():
                    try: done.exception()
                    except Exception: pass
            fut.add_done_callback(consume_late)
            self._bounded_write_reserved=True
            if payload.get('phase') in ('s08_policy_review_apply','s02_importance_prepare','s02_importance_apply'):
                if self._s08_policy_review_ticket is not None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('S08 review outcome requires original status')
                self._s08_policy_review_ticket=ticket
            if payload.get('phase')=='source_candidate':
                if self._paired_review_ticket is not None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('source publication outcome requires original status')
                prepared=self._guarded_preparation; original=payload['request']
                if prepared is None or transport.process is None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('source publication owner unavailable')
                ticket['source_publication_identity']={key:copy.deepcopy(original[key]) for key in ('version','owner','workspace_id','job_id','attempt_id','lease_epoch','source','policy_fingerprint')}
                ticket['source_publication_identity']['content_sha256']=hashlib.sha256(original['content'].encode('utf-8')).hexdigest()
                ticket['source_publication_process']=transport.process
                ticket['source_publication_database']=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)
                self._paired_review_ticket=ticket
            if payload.get('phase')=='paired_source_candidate':
                if self._paired_review_ticket is not None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('paired publication outcome requires original status')
                prepared=self._guarded_preparation; original=payload['request']
                if prepared is None or transport.process is None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('paired publication owner unavailable')
                ticket['paired_publication_identity']={key:copy.deepcopy(original[key]) for key in ('version','owner','workspace_id','job_id','attempt_id','lease_epoch','source','coverage','policy_fingerprint')}
                ticket['paired_publication_identity']['content_sha256']=hashlib.sha256(original['content'].encode('utf-8')).hexdigest()
                ticket['paired_publication_process']=transport.process
                ticket['paired_publication_database']=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)
                self._paired_review_ticket=ticket
            if payload.get('phase')=='pending_pair_rebind':
                if self._paired_review_ticket is not None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('pending pair outcome requires original status')
                prepared=self._guarded_preparation
                if prepared is None or transport.process is None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('pending pair owner unavailable')
                ticket['pending_pair_identity']=copy.deepcopy(payload['request'])
                ticket['pending_pair_process']=transport.process
                ticket['pending_pair_database']=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)
                self._paired_review_ticket=ticket
            if payload.get('phase')=='paired_review_apply':
                if self._paired_review_ticket is not None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('paired review outcome requires status')
                original=payload['request']
                ticket['paired_identity']={key:original[key] for key in ('owner','workspace_id','account_id','candidate_id','candidate_digest','association_sha256','request_key','reason','action')}
                self._paired_review_ticket=ticket
            if payload.get('phase') in ('explicit_add_review_apply','import_staged_review_apply'):
                if self._explicit_add_review_ticket is not None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('explicit Add review outcome requires status')
                original=payload['request']; selector=original['selector'];prepared=self._guarded_preparation
                if prepared is None or transport.process is None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('explicit Add review owner unavailable')
                ticket['explicit_add_process']=transport.process
                ticket['explicit_add_database']=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)
                ticket['explicit_add_identity']={**{key:selector[key] for key in ('owner','workspace_id','account_id','candidate_id')},
                    **{key:original[key] for key in ('expected_revision','expected_binding','review_key','action')}}
                self._explicit_add_review_ticket=ticket
            if payload.get('phase')=='import_review_apply':
                if self._import_review_ticket is not None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('import review outcome requires status')
                original=payload['request'];prepared=self._guarded_preparation
                if prepared is None or transport.process is None:
                    self._bounded_write_reserved=False
                    raise MemoryRequestRejectedError('import review owner unavailable')
                ticket['import_identity']={key:original[key] for key in ('owner','workspace_id','account_id','batch_id','item_id','suggestion_id','review_key_hash','proposal_fingerprint')}
                ticket['import_process']=transport.process;ticket['import_future']=fut
                ticket['import_database']=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)
                self._import_review_ticket=ticket
            try:
                self._assert_reset_handoff_access(_reset_handoff_token)
                fut._reset_handoff_token=_reset_handoff_token
                if _reset_handoff_token is not None: fut._reset_handoff_check=self._reserve_original_reset_issue(_reset_handoff_token,owner,workspace_id,payload,fut,ticket)
                def frame_check():
                    try:
                        self._assert_reset_handoff_access(_reset_handoff_token)
                        check=getattr(fut,'_reset_handoff_check',None)
                        sealed=(_reset_original is None or (reset_barrier.reset_witness_current(reset_witness) is True
                            and getattr(reset_claim._coordinator._reset_state,'account_request_barrier',None) is reset_barrier
                            and self._owned_transport is transport and self._owned_generation is generation
                            and transport.process is ticket['reset_process'] and self._owner_task is owner_task
                            and reset_claim.assert_current_post_dispatch(provider=self,frozen=reset_frozen) is True
                            and reset_issue_current() is True))
                        return (check is None or check() is True) and sealed
                    except Exception:return False
                ticket['frame_check']=frame_check
                ticket['on_unsent']=lambda:self._mark_original_reset_unsent_drained(fut)
            except BaseException:
                self._bounded_write_reserved=False
                if self._reset_handoff_state['original'] is ticket and not ticket['issued']:
                    ticket['may_issue']=False;ticket['reset_unsent_drained']=True
                    if not fut.done(): fut.cancel()
                if self._s08_policy_review_ticket is ticket and ticket.get('issued') is False:self._s08_policy_review_ticket=None
                if self._explicit_add_review_ticket is ticket:self._explicit_add_review_ticket=None
                if self._paired_review_ticket is ticket and ('pending_pair_identity' in ticket or 'paired_publication_identity' in ticket or 'source_publication_identity' in ticket):self._paired_review_ticket=None
                if self._import_review_ticket is ticket and ticket.get('issued') is False:
                    if not fut.done(): fut.cancel()
                    raise MemoryRequestRejectedError('bounded memory write unavailable') from None
                raise
            try:
                self._requests.put_nowait((_HOST_CANDIDATE_REVIEW, arguments, fut))
            except BaseException:
                self._bounded_write_reserved=False
                if self._reset_handoff_state['original'] is ticket and not ticket['issued']:
                    ticket['may_issue']=False;ticket['reset_unsent_drained']=True
                    if not fut.done(): fut.cancel()
                if self._s08_policy_review_ticket is ticket and ticket.get('issued') is False:self._s08_policy_review_ticket=None
                if self._explicit_add_review_ticket is ticket:self._explicit_add_review_ticket=None
                if self._paired_review_ticket is ticket and ('pending_pair_identity' in ticket or 'paired_publication_identity' in ticket or 'source_publication_identity' in ticket):self._paired_review_ticket=None
                if _reset_handoff_token is not None and _reset_handoff_token.issue_future is fut:
                    _reset_handoff_token.issue_drained.set()
                if self._import_review_ticket is ticket and ticket.get('issued') is False:
                    if not fut.done(): fut.cancel()
                    raise MemoryRequestRejectedError('bounded memory write unavailable') from None
                raise
            settled=False
            try:
                result=await self._settle_bounded_write(fut,owner_task,ticket)
                settled=True
            finally:
                # The ticket may retain an unknown identity, never a live credential/guard closure.
                ticket['before_issue']=None
                if 'queue_before_issue' in ticket: ticket['queue_before_issue']=None
                ticket['frame_check']=None
                if self._reset_handoff_state['original'] is ticket:
                    ticket['reset_io_settled']=bool(settled or (ticket.get('terminal_error') is True and not ticket.get('frame_invalid')))
                if ((self._reset_handoff_state['original'] is not ticket and _reset_handoff_token is None)
                    or ticket['issued'] or ticket.get('positive_unsent') is True or ticket.get('reset_unsent_drained') is True):
                    ticket['on_unsent']=None
                if self._s08_policy_review_ticket is ticket and ticket.get('issued') is False and ticket.get('owned_io_drained') is True:
                    self._s08_policy_review_ticket=None
                if self._paired_review_ticket is ticket:
                    if 'pending_pair_identity' in ticket or 'paired_publication_identity' in ticket or 'source_publication_identity' in ticket:
                        ticket['pending_pair_io_settled']=bool(settled or ticket.get('positive_unsent') is True or (ticket.get('terminal_error') is True and not ticket.get('frame_invalid')))
                        if ticket.get('positive_unsent') is True:self._paired_review_ticket=None
                    elif settled or not ticket['issued']:
                        self._paired_review_ticket=None
                if self._explicit_add_review_ticket is ticket:
                    ticket['io_settled']=bool(settled or (ticket.get('terminal_error') is True and not ticket.get('frame_invalid')))
                    if not ticket['issued']:self._explicit_add_review_ticket=None
        else:
            self._assert_reset_handoff_access(_reset_handoff_token)
            fut._reset_handoff_token=_reset_handoff_token
            try:
                self._requests.put_nowait((_HOST_CANDIDATE_REVIEW, arguments, fut))
            except asyncio.QueueFull as exc:
                raise MemoryRequestRejectedError("Memery worker queue is full", code="busy") from exc
        if write_deadline is None and require_live:
            done, _ = await asyncio.wait((fut, owner_task), return_when=asyncio.FIRST_COMPLETED)
            if fut not in done:
                raise MemoryRequestRejectedError('host-private source transport outcome unknown')
        if write_deadline is None:
            result = await fut
        if reset_feature and _reset_handoff_token is None and payload.get('phase')!='guarded_reset_commit':
            from src.openclank.mimo_supervisor import _queued_owned_thread
            await _queued_owned_thread(self._assert_reset_retirement_ready,None if reset_feature is True else reset_feature)
        if terminal_feature is not None and payload.get('phase')!='guarded_reset_commit':
            from src.openclank.mimo_supervisor import _queued_owned_thread
            await _queued_owned_thread(self._assert_terminal_s08_ready,terminal_feature)
        decoded, wire_size = result
        if payload.get('phase') in ('document_upsert','document_remove') and wire_size > 8192:
            raise MemoryRequestRejectedError('personal document receipt unavailable')
        if not isinstance(decoded, dict):
            raise MemoryRequestRejectedError("invalid host-private candidate response")
        if source_mapping is not None and _provider_source_mapping_error(self, source_mapping):
            raise MemoryRequestRejectedError('host source profile changed')
        return decoded

    def _s06_source_capability(self, db):
        """Verify exact inactive-source indexes; never infer missing coverage."""
        definitions = {
          's06_observation_origin_account_chat_message': "CREATE INDEX s06_observation_origin_account_chat_message ON fm_v2_change_sets(json_extract(operation_metadata_json, '$.source.account_id'),json_extract(operation_metadata_json, '$.source.chat_id'),json_extract(operation_metadata_json, '$.source.message_id'),owner_id,change_set_id) WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json)",
          's06_observation_origin_account': "CREATE INDEX s06_observation_origin_account ON fm_v2_change_sets(json_extract(operation_metadata_json, '$.source.account_id'),owner_id,change_set_id) WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json)",
          's06_observation_origin_account_chat': "CREATE INDEX s06_observation_origin_account_chat ON fm_v2_change_sets(json_extract(operation_metadata_json, '$.source.account_id'),json_extract(operation_metadata_json, '$.source.chat_id'),owner_id,change_set_id) WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json)",
          's06_observation_origin_invalid': "CREATE INDEX s06_observation_origin_invalid ON fm_v2_change_sets(owner_id,change_set_id) WHERE reason = 's06.observation-origin.v1' AND NOT json_valid(operation_metadata_json)",
          's06_observation_origin_invalid_account': "CREATE INDEX s06_observation_origin_invalid_account ON fm_v2_change_sets(owner_id,change_set_id) WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json) AND (json_type(operation_metadata_json, '$.source.account_id') IS NOT 'text' OR length(json_extract(operation_metadata_json, '$.source.account_id')) > 512 OR trim(json_extract(operation_metadata_json, '$.source.account_id')) != json_extract(operation_metadata_json, '$.source.account_id') OR json_extract(operation_metadata_json, '$.source.account_id') = '')",
          's06_observation_origin_owner_account': "CREATE INDEX s06_observation_origin_owner_account ON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json, '$.source.account_id'),change_set_id) WHERE reason = 's06.observation-origin.v1' AND json_valid(operation_metadata_json)",
          's06_found_source': "CREATE INDEX s06_found_source ON fm_v2_outbox(owner_id,json_extract(payload_json, '$.source_id'),event_id) WHERE kind = 's06_found' AND json_valid(payload_json)",
          's06_found_invalid': "CREATE INDEX s06_found_invalid ON fm_v2_outbox(owner_id,event_id) WHERE kind = 's06_found' AND NOT json_valid(payload_json)",
          's06_observation_rebind_current': "CREATE INDEX s06_observation_rebind_current ON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json, '$.origin_id'),json_extract(operation_metadata_json, '$.current_fingerprint'),change_set_id) WHERE reason = 's06.observation-rebind.v2' AND json_valid(operation_metadata_json)",
          's06_observation_rebind_successor': "CREATE INDEX s06_observation_rebind_successor\nON fm_v2_change_sets(owner_id,json_extract(operation_metadata_json, '$.origin_id'),\n  json_extract(operation_metadata_json, '$.prior_receipt_id'),change_set_id)\nWHERE reason = 's06.observation-rebind.v2' AND json_valid(operation_metadata_json)",
          's06_observation_rebind_invalid_json': "CREATE INDEX s06_observation_rebind_invalid_json ON fm_v2_change_sets(owner_id,change_set_id) WHERE reason = 's06.observation-rebind.v2' AND NOT json_valid(operation_metadata_json)",
          's06_observation_rebind_invalid_fields': "CREATE INDEX s06_observation_rebind_invalid_fields ON fm_v2_change_sets(owner_id,change_set_id)\nWHERE reason = 's06.observation-rebind.v2' AND json_valid(operation_metadata_json)\n  AND (json_type(operation_metadata_json, '$.origin_id') IS NOT 'text'\n       OR length(json_extract(operation_metadata_json, '$.origin_id')) != 77\n       OR substr(json_extract(operation_metadata_json, '$.origin_id'),1,13) != 'cs_s06origin_'\n       OR substr(json_extract(operation_metadata_json, '$.origin_id'),14) GLOB '*[^0-9a-f]*'\n       OR json_type(operation_metadata_json, '$.current_fingerprint') IS NOT 'text'\n       OR length(json_extract(operation_metadata_json, '$.current_fingerprint')) != 64\n       OR json_extract(operation_metadata_json, '$.current_fingerprint') GLOB '*[^0-9a-f]*'\n       OR (json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT 'null' AND json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT 'text')\n       OR (json_type(operation_metadata_json, '$.prior_receipt_hash') IS NOT 'null' AND json_type(operation_metadata_json, '$.prior_receipt_hash') IS NOT 'text')\n       OR (json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT json_type(operation_metadata_json, '$.prior_receipt_hash'))\n       OR (json_type(operation_metadata_json, '$.prior_receipt_id') = 'text' AND (length(json_extract(operation_metadata_json, '$.prior_receipt_id')) != 76\n          OR substr(json_extract(operation_metadata_json, '$.prior_receipt_id'),1,12) != 'cs_s06renew_'\n          OR substr(json_extract(operation_metadata_json, '$.prior_receipt_id'),13) GLOB '*[^0-9a-f]*'\n          OR length(json_extract(operation_metadata_json, '$.prior_receipt_hash')) != 64\n          OR json_extract(operation_metadata_json, '$.prior_receipt_hash') GLOB '*[^0-9a-f]*')))",
        }
        marks = ','.join('?' for _ in definitions)
        actual = {row['name']: row['sql'] for row in db.execute(f"SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN ({marks})",tuple(definitions))}
        canon = _canonical_index_sql
        if not actual:
            if db.execute("SELECT 1 FROM fm_v2_change_sets WHERE reason IN ('s06.observation-origin.v1','s06.observation-rebind.v2') LIMIT 1").fetchone():
                raise ValueError('observation source capability absent')
            return False
        if len(actual) != len(definitions) or any(canon(actual.get(name)) != canon(sql) for name,sql in definitions.items()):
            raise ValueError('observation source capability incomplete')
        return True

    def _s06_context_admitted_capability(self, db):
        sql = "CREATE INDEX s06_context_admitted_marker ON fm_v2_outbox(owner_id,event_id) WHERE kind='s06_admitted' AND json_valid(payload_json) AND json_extract(payload_json,'$.version')='s06.context-admitted.v1'"
        row = db.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND name='s06_context_admitted_marker'").fetchone()
        if row is None:
            if db.execute("SELECT 1 WHERE EXISTS(SELECT 1 FROM fm_v2_outbox WHERE kind='s06_admitted' AND json_valid(payload_json) AND json_extract(payload_json,'$.version')='s06.context-admitted.v1') OR EXISTS(SELECT 1 FROM fm_v2_change_sets WHERE reason='s06.admitted.v1' AND json_valid(operation_metadata_json) AND json_extract(operation_metadata_json,'$.version')='s06.context-admitted.v1')").fetchone():
                raise ValueError('context admitted capability absent')
            return False
        if row[0] != sql: raise ValueError('context admitted capability invalid')
        return True

    def _s06_admitted_capability(self, db):
        """Verify the optional admitted indexes without weakening found-only stores."""
        self._s06_context_admitted_capability(db)
        definitions = {
            's06_admitted_source': "CREATE INDEX s06_admitted_source ON fm_v2_outbox(owner_id,json_extract(payload_json, '$.source_id'),event_id) WHERE kind = 's06_admitted' AND json_valid(payload_json)",
            's06_admitted_invalid': "CREATE INDEX s06_admitted_invalid ON fm_v2_outbox(owner_id,event_id) WHERE kind = 's06_admitted' AND (NOT json_valid(payload_json) OR CASE WHEN json_valid(payload_json) THEN json_type(payload_json, '$.source_id') IS NOT 'text' OR length(json_extract(payload_json, '$.source_id')) != 73 OR substr(json_extract(payload_json, '$.source_id'),1,9) != 'hostpart_' OR substr(json_extract(payload_json, '$.source_id'),10) GLOB '*[^0-9a-f]*' ELSE 0 END)",
        }
        rows = {row['name']:row['sql'] for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN (?,?)", tuple(definitions))}
        if not rows:
            if (db.execute("SELECT 1 FROM fm_v2_outbox WHERE kind='s06_admitted' LIMIT 1").fetchone()
                    or db.execute("SELECT 1 FROM fm_v2_change_sets WHERE reason='s06.admitted.v1' LIMIT 1").fetchone()):
                raise ValueError('admitted capability absent')
            return False
        if len(rows) != len(definitions) or any(_canonical_index_sql(rows.get(name)) != _canonical_index_sql(sql) for name, sql in definitions.items()):
            raise ValueError('admitted capability invalid')
        return True

    def _s06_erase_capability(self, db):
        expected = "CREATE INDEX s06_source_erase ON fm_v2_change_sets(owner_id,source_event_id,change_set_id) WHERE reason IN ('s06.observation-origin.v1','s06.found.v1','s06.observation-rebind.v2','s06.admitted.v1')"
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='s06_source_erase'").fetchone()
        if row is None:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name IN ('s06_helped_source','s06_helped_invalid') LIMIT 1").fetchone():
                raise ValueError('partial helped capability invalid')
            return False
        if (_canonical_index_sql(row['sql']) == _canonical_index_sql(expected)
                and db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name IN ('s06_helped_source','s06_helped_invalid') LIMIT 1").fetchone()):
            raise ValueError('partial helped capability invalid')
        if _canonical_index_sql(row['sql']) != _canonical_index_sql(expected):
            extended="CREATE INDEX s06_source_erase ON fm_v2_change_sets(owner_id,source_event_id,change_set_id) WHERE reason IN ('s06.observation-origin.v1','s06.found.v1','s06.observation-rebind.v2','s06.admitted.v1','s06.helped.v1')"
            definitions={
                's06_helped_source': "CREATE INDEX s06_helped_source ON fm_v2_outbox(owner_id,json_extract(payload_json, '$.source_id'),event_id) WHERE kind = 's06_helped' AND json_valid(payload_json)",
                's06_helped_invalid': """CREATE INDEX s06_helped_invalid ON fm_v2_outbox(owner_id,event_id) WHERE kind = 's06_helped' AND (NOT json_valid(payload_json) OR CASE WHEN json_valid(payload_json) THEN json_type(payload_json, '$.source_id') IS NOT 'text' OR length(json_extract(payload_json, '$.source_id')) != 73 OR substr(json_extract(payload_json, '$.source_id'),1,9) != 'hostpart_' OR substr(json_extract(payload_json, '$.source_id'),10) GLOB '*[^0-9a-f]*' ELSE 0 END)""",
            }
            actual={r['name']:r['sql'] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN (?,?)",tuple(definitions))}
            if (_canonical_index_sql(row['sql']) != _canonical_index_sql(extended) or len(actual)!=len(definitions)
                    or any(_canonical_index_sql(actual.get(k))!=_canonical_index_sql(v) for k,v in definitions.items())):
                raise ValueError('source erasure capability invalid')
        return True

    def _maintenance_capability(self, db):
        definitions = {
            'derived_compaction_source': "CREATE INDEX derived_compaction_source ON fm_v2_evidence(owner_id,source_id,evidence_id) WHERE locator_type='derived_compaction_summary'",
            'derived_compaction_changes': "CREATE INDEX derived_compaction_changes ON fm_v2_change_sets(owner_id,source_event_id,change_set_id) WHERE reason='derived.compaction.v1'",
        }
        rows = {row['name']:row['sql'] for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN (?,?)",tuple(definitions))}
        if not rows:
            if (db.execute("SELECT 1 FROM fm_v2_evidence WHERE locator_type='derived_compaction_summary' LIMIT 1").fetchone()
                or db.execute("SELECT 1 FROM fm_v2_change_sets WHERE reason='derived.compaction.v1' LIMIT 1").fetchone()):
                raise ValueError('maintenance capability absent')
            return False
        if (len(rows) != len(definitions) or any(_canonical_index_sql(rows.get(name)) != _canonical_index_sql(sql) for name,sql in definitions.items())
            or not self._s06_source_capability(db) or not self._s06_erase_capability(db)):
            raise ValueError('maintenance capability invalid')
        return True

    def _reset_fence_binding(self, claim):
        from services.memory.nuke_coordinator import MemoryResetClaim
        from services.memory.skill_lifecycle import locked
        if type(claim) is not MemoryResetClaim:
            raise MemoryRequestRejectedError('reset fence authority unavailable')
        claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
        coordinator=claim._coordinator
        with locked(os.fspath(coordinator.lock_target)):
            row=coordinator._load_journal().get(claim.operation_id)
            participants=row.get('participants') if type(row) is dict else None
            if type(row) is dict and participants is None and coordinator.memory_provider is self:
                expected=row.get('expected')
                if (type(expected) is not dict or any(type(key) is not str for key in expected)
                    or any(row.get(key) is not None and row.get(key)!=claim.account_id
                           for key in ('full_account_id','selective_account_id'))):
                    raise MemoryRequestRejectedError('reset fence snapshot unavailable')
                components=sorted(key for key in expected if key in ('memories','graph','ingest'))
                fm={'components':components,'expected':{key:expected[key] for key in components}}
            else:
                fm=participants.get('fm') if type(participants) is dict else None
            if (type(fm) is not dict or type(fm.get('components')) is not list or type(fm.get('expected')) is not dict
                or not fm['components'] or len(fm['components'])>3 or len(set(fm['components']))!=len(fm['components'])
                or set(fm['components'])!=set(fm['expected']) or any(v not in ('memories','graph','ingest') for v in fm['components'])):
                raise MemoryRequestRejectedError('reset fence snapshot unavailable')
            result={'account_id':claim.account_id,'operation_id':claim.operation_id,
                    'components':list(fm['components']),'expected_counts':dict(fm['expected'])}
            if row.get('reset_s08_mode')=='terminal-s08-v1':
                queued=row.get('queued_expected')
                if (type(queued) is not dict or type(queued.get('count')) is not int or queued['count']!=0
                    or 'memories' not in row.get('components',[]) or claim._terminal_s08_witness is None
                    or claim._terminal_s08_witness is not self._terminal_s08_witness or not self._terminal_s08_ready_local()):
                    raise MemoryRequestRejectedError('terminal S08 reset authority unavailable')
                result['mode']='terminal-s08-v1'
            elif row.get('reset_s08_mode') not in (None,'no-s08-v1'):
                raise MemoryRequestRejectedError('terminal S08 reset authority unavailable')
        if len(json.dumps(result,sort_keys=True,separators=(',',':')).encode())>65536:
            raise MemoryRequestRejectedError('reset fence snapshot unavailable')
        claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
        return result

    async def _host_source_projection(self, request: _HostSourceProjection, *, fence: bool, rename_operation_id=None, reset_context=None) -> Dict[str, Any]:
        """Host-only source lifecycle operation; never exposed through generic tools."""
        from src.memery_external_sources import SourceRef
        import re
        if (type(request) is not _HostSourceProjection or type(request.source) is not SourceRef or type(fence) is not bool
            or _provider_source_ref_error(self, request.source)
            or (rename_operation_id is not None and (not fence or type(rename_operation_id) is not str or not rename_operation_id or rename_operation_id != rename_operation_id.strip() or len(rename_operation_id) > 512))
            or not all(isinstance(value,str) and value and value == value.strip() and len(value) <= 512
                       for value in (request.owner,request.workspace_id,request.job_id))
            or re.fullmatch(r'[0-9a-f]{64}',request.policy_fingerprint or '') is None):
            raise MemoryRequestRejectedError('host source projection unavailable')
        if self._scope(request.owner).owner != request.owner:
            raise MemoryRequestRejectedError('host source scope unavailable')
        phase = 'source_reset_fence' if reset_context else ('source_fence' if fence else 'source_project')
        payload = {'phase':phase,'version':1,'job_id':request.job_id,'source':asdict(request.source),'policy_fingerprint':request.policy_fingerprint}
        if rename_operation_id is not None:
            payload['rename_operation_id'] = rename_operation_id
        if reset_context:
            reset_context()
            payload['reset_operation_id'] = reset_context.operation_id
            payload['reset'] = await asyncio.to_thread(self._reset_fence_binding,reset_context.claim)
        result = await self._host_candidate_request(request.owner,request.workspace_id,payload, require_live=True,
            write_deadline=(float(asyncio.get_running_loop().time()+30.0) if reset_context else None),
            write_status=(reset_context.write_status if reset_context else None),
            reset_feature=(reset_context.closure.lease.reset_transport_witness if reset_context and reset_context.closure is not None else
                reset_context.claim._terminal_s08_witness[0] if reset_context and reset_context.claim._terminal_s08_witness is not None else bool(reset_context)),
            terminal_feature=(reset_context.claim._terminal_s08_witness if reset_context else None))
        if _provider_source_ref_error(self, request.source):
            raise MemoryRequestRejectedError('host source profile changed')
        if reset_context:
            reset_context()
            if await asyncio.to_thread(self._reset_fence_binding,reset_context.claim) != payload['reset']:
                raise MemoryRequestRejectedError('reset fence snapshot changed')
        ref = request.source
        identity = json.dumps([ref.version,ref.account_id,ref.chat_id,ref.actor_id,ref.message_id,ref.part_id],
                              ensure_ascii=False,separators=(',',':')).encode('utf-8')
        expected = 'hostpart_' + hashlib.sha256(identity).hexdigest()
        allowed = {'fenced','already_fenced','absent_fenced'} if fence else {'registered','already_active'}
        if rename_operation_id is not None:
            allowed.add('rename_absent')
        expected_fields = {'version','source_id','state','visibility','covered'} if fence else {'version','source_id','state'}
        if rename_operation_id is not None:
            expected_fields.add('rename_proof')
        if (set(result) != expected_fields or type(result['version']) is not int or result['version'] != 1
            or result['source_id'] != expected or result['state'] not in allowed):
            raise MemoryRequestRejectedError('invalid host source projection response')
        if fence:
            from src.openclank.memory_job_admission import _conn
            try:
                with _conn(self._local_db_path()) as db:
                    feedback = self._s06_source_capability(db)
                    context_admitted = self._s06_context_admitted_capability(db)
                    maintenance = self._maintenance_capability(db)
                    if context_admitted and (not feedback or self._guarded_preparation.capabilities.get('admitted') is not True):
                        raise ValueError('context admitted capability is inconsistent')
            except (ValueError, TypeError, sqlite3.Error) as exc:
                raise MemoryRequestRejectedError('host source capability unavailable') from exc
            expected_coverage = (['candidate','curated','knowledge','graph','digest','scoped_quality','reindex']
                + (['feedback'] if feedback else [])
                + (['admitted'] if feedback and self._guarded_preparation.capabilities.get('admitted') is True else [])
                + (['context_admitted'] if context_admitted and feedback and self._guarded_preparation.capabilities.get('admitted') is True else [])
                + (['maintenance'] if maintenance and feedback else []))
            if result['visibility'] != 'closed' or result['covered'] != expected_coverage:
                raise MemoryRequestRejectedError('host source visibility closure unavailable')
        if rename_operation_id is not None:
            proof = result['rename_proof']
            material = json.dumps([ref.account_id,rename_operation_id,expected],ensure_ascii=False,separators=(',',':')).encode('utf-8')
            expected_change = 'cs_s04rename_' + hashlib.sha256(material).hexdigest()
            if (type(proof) is not dict or set(proof) != {'change_set_id','active_versions'}
                or proof['change_set_id'] != expected_change or type(proof['active_versions']) is not list
                or len(proof['active_versions']) > 128 or (result['state'] == 'rename_absent' and proof['active_versions'])):
                raise MemoryRequestRejectedError('host source rename proof unavailable')
            previous = 0
            for item in proof['active_versions']:
                if (type(item) is not dict or set(item) != {'revision','content_hash'}
                    or type(item['revision']) is not int or item['revision'] <= previous
                    or type(item['content_hash']) is not str or re.fullmatch(r'[0-9a-f]{64}',item['content_hash']) is None):
                    raise MemoryRequestRejectedError('host source rename proof unavailable')
                previous = item['revision']
        return result

    async def _host_source_project(self, request: _HostSourceProjection) -> Dict[str, Any]:
        return await self._host_source_projection(request, fence=False)

    async def _host_selective_ingest_partition(self, *, claim, target_authority, barrier, witness, deadline):
        """Read a bounded partition under the original claim and drained account barrier."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.account_request_barrier import AccountRequestBarrier, _ResetDrainWitness
        from src.openclank.memory_job_admission import TargetAuthority
        from src.openclank.mimo_supervisor import _queued_owned_thread
        if (type(claim) is not MemoryResetClaim or type(target_authority) is not TargetAuthority
            or type(barrier) is not AccountRequestBarrier or type(witness) is not _ResetDrainWitness
            or getattr(claim._coordinator._reset_state,'account_request_barrier',None) is not barrier
            or witness.barrier is not barrier or witness.owner!=barrier._owner(claim.owner)):
            raise MemoryRequestRejectedError('selective ingest authority unavailable')
        async def current():
            claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
            if barrier.reset_witness_current(witness) is not True:
                raise MemoryRequestRejectedError('selective ingest authority unavailable')
            await _queued_owned_thread(self._assert_reset_retirement_ready)
            binding=await _queued_owned_thread(target_authority.resolve,claim.account_id)
            claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
            if (getattr(claim._coordinator._reset_state,'account_request_barrier',None) is not barrier
                or barrier.reset_witness_current(witness) is not True
                or binding.memory_owner!=self._scope(binding.memory_owner).owner or binding.memory_workspace!=self._workspace_id):
                raise MemoryRequestRejectedError('selective ingest target changed')
            return binding
        before=await current();reset=await asyncio.to_thread(self._reset_fence_binding,claim)
        if 'memories' in reset.get('components',[]) or 'ingest' not in reset.get('components',[]):
            raise MemoryRequestRejectedError('selective ingest scope unavailable')
        selective=reset.get('expected_counts',{}).get('ingest',{}).get('selective')
        from src.openclank.fm_reset_receipt import validate_selective_ingest_summary
        try:
            validate_selective_ingest_summary(selective)
        except ValueError as exc:
            raise MemoryRequestRejectedError('selective ingest preview unavailable') from exc
        result=await self._s07_owner_rpc(before.memory_owner,before.memory_workspace,{'phase':'selective_ingest_partition','version':1,'account_id':claim.account_id,'partition_sha256':selective['partition_sha256']},deadline,read_ticket={})
        fields={'version','scope','all_s05_sources_retained','binding_sha256','imports','selectors'}
        names={'retained_jobs','erasable_jobs','terminal_erased_jobs','retained_candidates','erasable_candidates','generic_retained_candidates','generic_erasable_candidates','s08_retained_candidates','s08_erasable','retained_sources','erasable_sources','retained_raw','erasable_raw','retained_recovery','erasable_recovery','retained_quarantine','erasable_quarantine'}
        def digest(value):
            return type(value) is str and re.fullmatch(r'[0-9a-f]{64}',value) is not None
        def hashes(value):
            return type(value) is list and all(digest(v) for v in value) and value==sorted(set(value))
        if (type(result) is not dict or set(result)!=fields or type(result['version']) is not int or result['version']!=2
            or result['scope']!='selective_ingest' or result['all_s05_sources_retained'] is not True
            or result['binding_sha256']!=selective['partition_sha256'] or type(result['selectors']) is not dict
            or set(result['selectors'])!=names):
            raise MemoryRequestRejectedError('selective ingest partition unavailable')
        selected=result['selectors']
        if any(not hashes(selected[name]) for name in names-{'s08_erasable'}) or selected['erasable_sources']!=[]:
            raise MemoryRequestRejectedError('selective ingest partition unavailable')
        triples=selected['s08_erasable']
        if (type(triples) is not list or any(type(v) is not dict or set(v)!={'job_selector','candidate_selector','change_selector'}
            or any(not digest(x) for x in v.values()) for v in triples)):
            raise MemoryRequestRejectedError('selective ingest partition unavailable')
        tuples=[(v['job_selector'],v['candidate_selector'],v['change_selector']) for v in triples]
        if tuples!=sorted(set(tuples)) or any(len({v[i] for v in tuples})!=len(tuples) for i in range(3)):
            raise MemoryRequestRejectedError('selective ingest partition unavailable')
        imports=result['imports']
        import_fields={'version','owner','account_id','retained_job_selectors','erasable_job_selectors','job_bindings','retained_staging','erasable_staging','approved_refs','snapshot_claims','immutable_event_job_selectors','retained_content_job_selectors','retained_candidate_selectors','erasable_candidate_selectors','candidate_bindings','digest'}
        if (type(imports) is not dict or set(imports)!=import_fields or type(imports['version']) is not int or imports['version']!=2
            or imports['owner']!=before.memory_owner or imports['account_id']!=claim.account_id or not digest(imports['digest'])
            or any(not hashes(imports[k]) for k in ('retained_job_selectors','erasable_job_selectors','immutable_event_job_selectors','retained_content_job_selectors','retained_candidate_selectors','erasable_candidate_selectors'))):
            raise MemoryRequestRejectedError('selective ingest import partition unavailable')
        def rows(name, keys, text_fields, hash_fields):
            values=imports[name]
            return (type(values) is list and all(type(v) is dict and set(v)==keys
                and all(type(v[k]) is str for k in text_fields) and all(digest(v[k]) for k in hash_fields) for v in values))
        if not rows('job_bindings',{'job_selector','workspace_id','kind','idempotency_sha256','row_sha256','relation_sha256'},('workspace_id','kind'),('job_selector','idempotency_sha256','row_sha256','relation_sha256')):
            raise MemoryRequestRejectedError('selective ingest import partition unavailable')
        if (not rows('candidate_bindings',{'candidate_selector','job_selector','state','proof_sha256'},('state',),('candidate_selector','job_selector','proof_sha256'))
            or any(v['state'] not in ('pending','rejected','accepted') for v in imports['candidate_bindings'])
            or set(imports['retained_candidate_selectors']) & set(imports['erasable_candidate_selectors'])
            or {v['candidate_selector'] for v in imports['candidate_bindings']} != set(imports['retained_candidate_selectors']) | set(imports['erasable_candidate_selectors'])
            or len({v['candidate_selector'] for v in imports['candidate_bindings']}) != len(imports['candidate_bindings'])
            or any(v['state']=='accepted' and v['candidate_selector'] in imports['erasable_candidate_selectors'] for v in imports['candidate_bindings'])):
            raise MemoryRequestRejectedError('selective ingest import partition unavailable')
        for name in ('retained_staging','erasable_staging'):
            if (not rows(name,{'workspace_id','batch_id','staged_item_id','source_id_sha256','content_sha256','byte_size','job_selectors'},('workspace_id','batch_id','staged_item_id'),('source_id_sha256','content_sha256'))
                or any(type(v['byte_size']) is not int or v['byte_size']<0 or not hashes(v['job_selectors']) for v in imports[name])):
                raise MemoryRequestRejectedError('selective ingest import partition unavailable')
        if (not rows('approved_refs',{'workspace_id','job_selector','suggestion_sha256','publication_sha256','publication_kind','review_key_hash','receipt_id_sha256','receipt_sha256'},('workspace_id','publication_kind'),('job_selector','suggestion_sha256','publication_sha256','review_key_hash','receipt_id_sha256','receipt_sha256'))
            or not rows('snapshot_claims',{'workspace_id','job_selector','item_id','account','upload_id','scope'},('workspace_id','item_id','account','upload_id','scope'),('job_selector',))):
            raise MemoryRequestRejectedError('selective ingest import partition unavailable')
        s08_erased={v[1] for v in tuples}
        kept=selected['retained_candidates']+selected['generic_retained_candidates']+selected['s08_retained_candidates']
        erased=selected['erasable_candidates']+selected['generic_erasable_candidates']+list(s08_erased)
        if (len(kept)!=len(set(kept)) or len(erased)!=len(set(erased)) or set(kept)&set(erased)):
            raise MemoryRequestRejectedError('selective candidate partition changed')
        if (set(selected['retained_jobs']) & (set(selected['erasable_jobs']) | set(selected['terminal_erased_jobs']))
            or set(selected['erasable_jobs']) & set(selected['terminal_erased_jobs'])
            or set(selected['retained_candidates']) & set(selected['erasable_candidates'])
            or set(selected['generic_retained_candidates']) & (set(selected['erasable_candidates']) | s08_erased)
            or set(selected['s08_retained_candidates']) & (set(selected['erasable_candidates']) | s08_erased)
            or set(selected['erasable_candidates']) & s08_erased
            or len(kept)!=selective['retained_approved_candidates']
            or len(erased)!=selective['erasable_candidates']
            or len(selected['retained_raw'])!=selective['retained_raw']['count']
            or set(selected['retained_raw']) & set(selected['erasable_raw'])
            or set(selected['retained_recovery']) & set(selected['erasable_recovery'])
            or len(selected['retained_recovery'])!=selective['retained_recovery']['count']
            or len(selected['retained_quarantine'])!=selective['retained_quarantine']['count']
            or len(imports['retained_content_job_selectors'])!=selective['retained_jobs']['count']
            or not set(imports['retained_content_job_selectors']).issubset(imports['retained_job_selectors'])
            or set(selected['retained_quarantine']) & set(selected['erasable_quarantine'])):
            raise MemoryRequestRejectedError('selective ingest partition changed')
        if await asyncio.to_thread(self._reset_fence_binding,claim)!=reset or await current()!=before:
            raise MemoryRequestRejectedError('selective ingest authority changed')
        return result

    async def _host_queued_origin_preflight(self, checks, *, claim, inventory, target_authority, reset_lease=None):
        """Read-only, exact verified source set; result is not a mutation grant."""
        from dataclasses import asdict
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.memery_external_sources import CurrentQueuedSource
        from src.openclank.conversation_archive import QueuedProducerInventory
        from src.openclank.memory_job_admission import TargetAuthority
        from src.openclank.queued_source import QueuedSourceRef
        if (type(claim) is not MemoryResetClaim or type(inventory) is not QueuedProducerInventory
            or inventory.claim is not claim or inventory.provider is not self
            or (reset_lease is not None and (reset_lease.claim is not claim or reset_lease.inventory is not inventory))
            or type(target_authority) is not TargetAuthority or type(checks) is not list or not 1 <= len(checks) <= 128):
            raise MemoryRequestRejectedError('queued preflight unavailable')
        def check_claim():
            claim.assert_current(owner=inventory.owner, account_id=inventory.account_id,
                                 provider=self, operation_id=inventory.operation_id)
            inventory.assert_current()
            if reset_lease is not None:
                target_authority._supervisor.assert_queued_reset_lease(reset_lease,claim=claim)
        return await self._host_queued_origin_preflight_read(checks, owner=inventory.owner,
            account_id=inventory.account_id, payload_json=inventory.payload_json,
            target_authority=target_authority, check_authority=check_claim, reset_lease=reset_lease)

    async def _host_queued_origin_preflight_read(self, checks, *, owner, account_id, payload_json,
                                                  target_authority, check_authority, reset_lease=None):
        """Shared current-source/FM read core; caller retains its authority."""
        from dataclasses import asdict
        from src.memery_external_sources import CurrentQueuedSource
        from src.openclank.queued_source import QueuedSourceRef
        try:
            if not callable(check_authority) or type(checks) is not list or not 1 <= len(checks) <= 128:
                raise ValueError('queued preflight authority unavailable')
            async def check():
                if reset_lease is None: await asyncio.to_thread(check_authority)
                else: await target_authority._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
            await check()
            def resolve():
                return (target_authority.resolve(account_id) if reset_lease is None else
                    target_authority._resolve_queued_reset(account_id,lease=reset_lease,claim=reset_lease.claim))
            binding=resolve()
            if self._scope(binding.memory_owner).owner != binding.memory_owner:
                raise ValueError('queued target unavailable')
            payload=json.loads(payload_json)
            expected={row['sourceIdentitySHA256'] for row in payload['sources'] if row['association']=='verified'}
            if len(expected)!=len(payload['sources']) or len(expected)!=len(checks):
                raise ValueError('queued source set unavailable')
            pool=target_authority._supervisor
            sources=[];verifiers=[];seen=set()
            for item in checks:
                if (type(item) is not dict or set(item) not in ({'source','guard','preimage','verifier'},
                    {'source','guard','fence','verifier'}) or ('fence' in item and reset_lease is None)):
                    raise ValueError('queued source unavailable')
                source=item['source']; verifier=item['verifier']
                if (type(source) is not QueuedSourceRef or type(verifier) is not CurrentQueuedSource
                    or verifier._provider is not self or verifier._target is not target_authority
                    or verifier._owner != owner or source.account_id != account_id
                    or (reset_lease is None and pool.worker_for_owner(owner) is not verifier._worker)
                    or (reset_lease is not None and pool.queued_reset_lease_for_source(owner,verifier._bridge,source) is not reset_lease)):
                    raise ValueError('queued source authority unavailable')
                identity=hashlib.sha256(json.dumps(['queued.producer-source.v1',source.account_id,source.chat_id,
                    source.native_session_id,source.request_id,source.message_id,source.part_id],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
                if identity in seen or identity not in expected: raise ValueError('queued source set unavailable')
                seen.add(identity)
                def epoch(v=verifier,s=source):
                    if reset_lease is not None:
                        return reset_lease.epoch if pool.queued_reset_lease_for_source(owner,v._bridge,s) is reset_lease else None
                    if pool.worker_for_owner(owner) is not v._worker or not v._worker.is_alive(): return None
                    value=pool._owner_lifecycle_epoch(owner)
                    pool._assert_owner_lifecycle(owner,value)
                    return value
                if 'fence' in item:
                    evidence=pool.queued_retirement_evidence(owner,verifier._bridge,claim=reset_lease.claim,source_hash=identity)
                    if evidence is None: raise ValueError('queued source evidence unavailable')
                    verify,_=verifier._terminal_source_verifier(source,item['fence'],guard=item['guard'],evidence=evidence,
                        lease_current=lambda v=verifier,s=source: pool.queued_reset_lease_for_source(owner,v._bridge,s) is reset_lease,
                        reset_lease=reset_lease)
                    verifiers.append(verify)
                else:
                    options={'guard':item['guard'],'preimage':item['preimage'],'producer_epoch':epoch}
                    if reset_lease is not None: options['reset_lease']=reset_lease
                    verifiers.append(verifier._current_source_verifier(source,**options))
                sources.append(source)
            if seen!=expected: raise ValueError('queued source set unavailable')
            for verify in verifiers: await verify()
            await check()
            result=await self._host_candidate_request(binding.memory_owner,binding.memory_workspace,
                {'phase':'queued_origin_preflight','version':1,'account_id':account_id,
                 'sources':[asdict(source) for source in sources]},require_live=True,
                reset_feature=(reset_lease.reset_transport_witness if reset_lease is not None else True))
            for verify in verifiers: await verify()
            await check()
            if resolve()!=binding: raise ValueError('queued target changed')
            fields=('version','kind','account_id','chat_id','workspace_id','request_id','native_session_id','message_id','part_id',
                    'archive_revision','archive_content_hash','text_sha256','ingress_sha256','root_operation_id','execution_revision','inner_turn_id')
            material=[[getattr(source,name) for name in fields] for source in sources]
            def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            request_hash=digest(['openclank.queued-preflight.v1',binding.memory_owner,binding.memory_workspace,account_id,material])
            if (type(result) is not dict or set(result)!={'version','request_sha256','fingerprint','items'}
                or type(result['version']) is not int or result['version']!=1 or result['request_sha256']!=request_hash
                or type(result['items']) is not list or len(result['items'])!=len(sources)):
                raise ValueError('queued preflight receipt unavailable')
            for source,item in zip(sources,result['items']):
                source_id='queuedpart_'+digest(['openclank.queued-source.v1',source.account_id,source.chat_id,source.native_session_id,source.request_id,source.message_id,source.part_id])
                origin_id='cs_s04queued_'+digest([source_id,source.archive_revision])
                if (type(item) is not dict or set(item)!={'source_id','origin_id','state'}
                    or item['source_id']!=source_id or item['origin_id']!=origin_id or item['state'] not in ('active','fenced')):
                    raise ValueError('queued preflight receipt unavailable')
            result_material=[[item['source_id'],item['origin_id'],item['state']] for item in result['items']]
            if result['fingerprint']!=digest(['openclank.queued-preflight.result.v1',request_hash,result_material]):
                raise ValueError('queued preflight receipt unavailable')
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise MemoryRequestRejectedError('queued preflight unavailable') from exc

    async def _host_queued_origin_fence(self, source, *, mapping, guard, target_authority, reset_lease=None):
        if reset_lease is None:
            return await self._host_queued_origin_fence_inner(source,mapping=mapping,guard=guard,target_authority=target_authority)
        from services.memory.skill_lifecycle import locked
        claim=reset_lease.claim;coordinator=claim._coordinator
        raw=json.dumps(['queued.producer-source.v1',source.account_id,source.chat_id,source.native_session_id,
            source.request_id,source.message_id,source.part_id],ensure_ascii=False,separators=(',',':')).encode()
        source_hash=hashlib.sha256(raw).hexdigest();status={'ticket':None}
        if source_hash not in reset_lease.source_hashes:
            raise MemoryRequestRejectedError('queued reset source is outside the original manifest')
        def transition(receipt=None,no_send=False):
            claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
            with locked(os.fspath(coordinator.lock_target)):
                operations=coordinator._load_journal();row=operations.get(claim.operation_id)
                records=row.get('reset_queued_receipts') if type(row) is dict else None
                if type(records) is not dict or type(row.get('reset_source_manifest')) is not dict:
                    raise MemoryRequestRejectedError('queued reset progress unavailable')
                previous=records.get(source_hash)
                if no_send:
                    if previous!={'state':'started'}: raise MemoryRequestRejectedError('queued reset progress changed')
                    records.pop(source_hash);coordinator._save_journal(operations);return None
                if receipt is None:
                    if type(previous) is dict and previous.get('state')=='complete': return previous.get('receipt')
                    if previous=={'state':'started'}: return 'reconcile'
                    if previous is not None: raise MemoryRequestRejectedError('queued reset progress unavailable')
                    remaining=getattr(reset_lease,'reset_invocation_remaining',None);attempts=row.get('reset_fence_attempts')
                    if type(remaining) is not int or remaining<1:
                        raise MemoryRequestRejectedError('reset fence invocation deferred; retry the same operation')
                    if type(attempts) is not int or attempts<0: raise MemoryRequestRejectedError('queued reset progress unavailable')
                    records[source_hash]={'state':'started'};row['reset_fence_attempts']=attempts+1
                    coordinator._save_journal(operations);reset_lease.reset_invocation_remaining=remaining-1
                    return None
                if previous!={'state':'started'}: raise MemoryRequestRejectedError('queued reset progress changed')
                records[source_hash]={'state':'complete','receipt':receipt};coordinator._save_journal(operations)
        async def run():
            pool=target_authority._supervisor;mapping.validate_chat_guard(source.chat_id,guard)
            await pool.validate_queued_reset_lease(reset_lease,claim=claim,source_hash=source_hash)
            prior=await asyncio.to_thread(transition)
            if prior is not None:
                proof=await self._read_queued_reset_fence(source,mapping=mapping,guard=guard,target_authority=target_authority,lease=reset_lease)
                if type(prior) is dict:
                    if any(prior.get(k)!=proof.get(k) for k in ('version','source_id','origin_id','source','visibility','covered')):
                        raise MemoryRequestRejectedError('queued reset fence changed')
                    return prior
                await asyncio.to_thread(transition,proof);return proof
            try:
                receipt=await self._host_queued_origin_fence_inner(source,mapping=mapping,guard=guard,target_authority=target_authority,
                    reset_lease=reset_lease,write_status=status)
            except BaseException:
                ticket=status['ticket']
                if ticket is None or not ticket['issued']: await asyncio.to_thread(transition,None,True)
                raise
            await asyncio.to_thread(transition,receipt);return receipt
        task=asyncio.create_task(run())
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            task.result();raise

    async def _read_queued_reset_fence(self, source, *, mapping, guard, target_authority, lease):
        from src.openclank.mimo_supervisor import _queued_owned_thread
        await _queued_owned_thread(self._assert_reset_retirement_ready,lease.reset_transport_witness)
        pool=target_authority._supervisor;claim=lease.claim
        mapping.validate_chat_guard(source.chat_id,guard)
        await pool.validate_queued_reset_lease(lease,claim=claim)
        binding=target_authority._resolve_queued_reset(source.account_id,lease=lease,claim=claim)
        reset=await asyncio.to_thread(self._reset_fence_binding,claim)
        payload={'phase':'queued_origin_reset_fence_read','version':1,'source':asdict(source),'reset':reset}
        if not self._reset_retirement_ready_local() or self._reset_retirement_witness is not lease.reset_transport_witness: raise MemoryRequestRejectedError('queued reset live capability changed')
        result=await self._s07_owner_rpc(binding.memory_owner,binding.memory_workspace,payload,asyncio.get_running_loop().time()+6.0)
        raw=json.dumps(['openclank.queued-source.v1',source.account_id,source.chat_id,source.native_session_id,
            source.request_id,source.message_id,source.part_id],ensure_ascii=False,separators=(',',':')).encode()
        source_id='queuedpart_'+hashlib.sha256(raw).hexdigest()
        origin_id='cs_s04queued_'+hashlib.sha256(json.dumps([source_id,source.archive_revision],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if (type(result) is not dict or set(result)!={'version','source_id','origin_id','source','state','visibility','covered'}
            or type(result['version']) is not int or result['version']!=1 or result['source_id']!=source_id
            or result['origin_id']!=origin_id or result['source']!=asdict(source) or result['state']!='already_fenced'
            or result['visibility']!='closed' or result['covered']!=['queued_origin_only']):
            raise MemoryRequestRejectedError('queued reset outcome requires reconciliation')
        await _queued_owned_thread(self._assert_reset_retirement_ready,lease.reset_transport_witness)
        mapping.validate_chat_guard(source.chat_id,guard);await pool.validate_queued_reset_lease(lease,claim=claim)
        if target_authority._resolve_queued_reset(source.account_id,lease=lease,claim=claim)!=binding or await asyncio.to_thread(self._reset_fence_binding,claim)!=reset:
            raise MemoryRequestRejectedError('queued reset fence proof changed')
        return result

    async def _host_queued_origin_fence_inner(self, source, *, mapping, guard, target_authority, reset_lease=None, write_status=None):
        """Fence only the proven origin-only cut; caller owns current-source proof."""
        from dataclasses import asdict
        from src.openclank.queued_source import QueuedSourceRef
        from src.openclank.session_map import OwnerSessionMap
        from src.openclank.memory_job_admission import TargetAuthority
        if type(source) is not QueuedSourceRef or type(mapping) is not OwnerSessionMap or type(target_authority) is not TargetAuthority:
            raise MemoryRequestRejectedError('queued source authority unavailable')
        mapping.validate_chat_guard(source.chat_id, guard)
        def resolve():
            return (target_authority.resolve(source.account_id) if reset_lease is None else
                target_authority._resolve_queued_reset(source.account_id,lease=reset_lease,claim=reset_lease.claim))
        if reset_lease is not None and target_authority._supervisor.queued_reset_lease_for_source(reset_lease.owner,reset_lease.bridge,source) is not reset_lease:
            raise MemoryRequestRejectedError('queued reset source unavailable')
        if reset_lease is not None:
            await target_authority._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
        binding=resolve()
        if self._scope(binding.memory_owner).owner != binding.memory_owner:
            raise MemoryRequestRejectedError('queued target unavailable')
        if reset_lease is not None:
            if not target_authority._supervisor.queued_reset_lease_current(reset_lease,claim=reset_lease.claim):
                raise MemoryRequestRejectedError('queued reset source unavailable')
        payload={'phase':'queued_origin_fence','version':1,'source':asdict(source)}
        if reset_lease is not None:
            payload['reset']=await asyncio.to_thread(self._reset_fence_binding,reset_lease.claim)
            reset_lease.touched=True
        result=await self._host_candidate_request(binding.memory_owner,binding.memory_workspace,payload,require_live=True,
            write_deadline=(float(asyncio.get_running_loop().time()+30.0) if reset_lease is not None else None),write_status=write_status,
            reset_feature=(reset_lease.reset_transport_witness if reset_lease is not None else False),
            terminal_feature=(reset_lease.claim._terminal_s08_witness if reset_lease is not None and payload['reset'].get('mode')=='terminal-s08-v1' else None))
        if reset_lease is not None:
            await target_authority._supervisor.validate_queued_reset_lease(reset_lease,claim=reset_lease.claim)
            if await asyncio.to_thread(self._reset_fence_binding,reset_lease.claim)!=payload['reset']:
                raise MemoryRequestRejectedError('reset fence snapshot changed')
        mapping.validate_chat_guard(source.chat_id, guard)
        if resolve()!=binding:
            raise MemoryRequestRejectedError('queued target changed')
        raw=json.dumps(['openclank.queued-source.v1',source.account_id,source.chat_id,source.native_session_id,
            source.request_id,source.message_id,source.part_id],ensure_ascii=False,separators=(',',':')).encode()
        source_id='queuedpart_'+hashlib.sha256(raw).hexdigest()
        raw=json.dumps([source_id,source.archive_revision],ensure_ascii=False,separators=(',',':')).encode()
        origin_id='cs_s04queued_'+hashlib.sha256(raw).hexdigest()
        if (type(result) is not dict or set(result)!={'version','source_id','origin_id','source','state','visibility','covered'}
            or type(result['version']) is not int or result['version']!=1 or result['source_id']!=source_id
            or result['origin_id']!=origin_id or result['source']!=asdict(source)
            or result['state'] not in ('fenced','already_fenced') or result['visibility']!='closed'
            or result['covered']!=['queued_origin_only']):
            raise MemoryRequestRejectedError('queued fence receipt unavailable')
        return result

    async def _host_source_fence(self, request, *, rename_operation_id=None, reset_claim=None, mapping=None, guard=None, target_authority=None, reset_closure=None) -> Dict[str, Any]:
        if reset_closure is None:
            return await self._host_source_fence_inner(request,rename_operation_id=rename_operation_id,reset_claim=reset_claim,
                mapping=mapping,guard=guard,target_authority=target_authority,reset_closure=reset_closure)
        from src.openclank.chat_source_bridge import _reset_ref_digest
        from services.memory.skill_lifecycle import locked
        digest=_reset_ref_digest(request);lease=reset_closure.lease;coordinator=reset_claim._coordinator
        status={'ticket':None}
        def transition(receipt=None, no_send=False):
            reset_claim.assert_current(owner=reset_claim.owner,account_id=reset_claim.account_id,provider=self,operation_id=reset_claim.operation_id)
            with locked(os.fspath(coordinator.lock_target)):
                operations=coordinator._load_journal();row=operations.get(reset_claim.operation_id)
                manifest=row.get('reset_source_manifest') if type(row) is dict else None
                records=row.get('reset_source_receipts') if type(row) is dict else None
                if (type(manifest) is not dict or type(records) is not dict or
                    not any(digest in root.get('refs',[]) for root in manifest.get('roots',[]) if type(root) is dict)):
                    raise MemoryRequestRejectedError('reset source is outside the original manifest')
                previous=records.get(digest)
                if no_send:
                    if previous!={'state':'started'}: raise MemoryRequestRejectedError('reset source progress changed')
                    records.pop(digest);coordinator._save_journal(operations);return None
                if receipt is None:
                    if type(previous) is dict and previous.get('state')=='complete': return previous.get('receipt')
                    if previous=={'state':'started'}: return 'reconcile'
                    if previous is not None: raise MemoryRequestRejectedError('reset source outcome requires reconciliation')
                    remaining=getattr(lease,'reset_invocation_remaining',None)
                    if type(remaining) is not int or remaining<1:
                        raise MemoryRequestRejectedError('reset fence invocation deferred; retry the same operation')
                    attempts=row.get('reset_fence_attempts')
                    if type(attempts) is not int or attempts<0: raise MemoryRequestRejectedError('reset source progress unavailable')
                    records[digest]={'state':'started'};row['reset_fence_attempts']=attempts+1
                    coordinator._save_journal(operations);lease.reset_invocation_remaining=remaining-1
                    return None
                if previous!={'state':'started'}: raise MemoryRequestRejectedError('reset source progress changed')
                records[digest]={'state':'complete','receipt':receipt};coordinator._save_journal(operations)
        async def run():
            mapping.validate_chat_guard(request.source.chat_id,guard)
            await target_authority._supervisor.validate_queued_reset_closure(reset_closure,claim=reset_claim)
            target=target_authority._resolve_stopped_reset(reset_claim.account_id,closure=reset_closure,claim=reset_claim)
            if (request.source.account_id!=reset_claim.account_id or request.owner!=target.memory_owner
                or request.workspace_id!=target.memory_workspace):
                raise MemoryRequestRejectedError('reset source target changed')
            prior=await asyncio.to_thread(transition)
            if type(prior) is dict:
                proof=await self._read_reset_source_fence(request,claim=reset_claim,mapping=mapping,guard=guard,
                    target_authority=target_authority,closure=reset_closure)
                if any(prior.get(k)!=proof.get(k) for k in ('version','source_id','visibility','covered')):
                    raise MemoryRequestRejectedError('reset source fence changed')
                return prior
            if prior=='reconcile':
                receipt=await self._read_reset_source_fence(request,claim=reset_claim,mapping=mapping,guard=guard,
                    target_authority=target_authority,closure=reset_closure)
            else:
                try:
                    receipt=await self._host_source_fence_inner(request,rename_operation_id=rename_operation_id,reset_claim=reset_claim,
                        mapping=mapping,guard=guard,target_authority=target_authority,reset_closure=reset_closure,write_status=status)
                except BaseException:
                    ticket=status['ticket']
                    if ticket is None or not ticket['issued']:
                        await asyncio.to_thread(transition,None,True)
                    raise
            await asyncio.to_thread(transition,receipt)
            return receipt
        task=asyncio.create_task(run())
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            task.result();raise

    async def _read_reset_source_fence(self, request, *, claim, mapping, guard, target_authority, closure):
        """Prove an exact prior fence without replaying an unknown write."""
        from src.openclank.chat_source_bridge import _source_fence_covered
        from src.openclank.mimo_supervisor import _queued_owned_thread
        await _queued_owned_thread(self._assert_reset_retirement_ready,closure.lease.reset_transport_witness)
        pool=target_authority._supervisor
        async def current():
            mapping.validate_chat_guard(request.source.chat_id,guard)
            await pool.validate_queued_reset_closure(closure,claim=claim)
            binding=target_authority._resolve_stopped_reset(claim.account_id,closure=closure,claim=claim)
            if (binding.memory_owner!=request.owner or binding.memory_workspace!=request.workspace_id
                or request.source.account_id!=claim.account_id):
                raise MemoryRequestRejectedError('reset source proof authority changed')
            return binding
        before=await current();reset=await asyncio.to_thread(self._reset_fence_binding,claim)
        if type(request) is _HostSourceProjection:
            payload={'phase':'source_reset_fence_read','version':1,'job_id':request.job_id,
                'policy_fingerprint':request.policy_fingerprint,'source':asdict(request.source),'reset':reset}
        elif type(request) is _HostObservationProjection:
            authority={'origin_id':request.origin_id,'origin_hash':request.origin_hash,'captured_owner':request.captured_owner,
                'current_fingerprint':request.current_fingerprint,'rebind_id':request.rebind_id,'rebind_hash':request.rebind_hash}
            payload={'phase':'observation_reset_fence_read','version':1,'authority':authority,'source':asdict(request.source),'reset':reset}
        else: raise MemoryRequestRejectedError('reset source proof unavailable')
        if not self._reset_retirement_ready_local() or self._reset_retirement_witness is not closure.lease.reset_transport_witness: raise MemoryRequestRejectedError('queued reset live capability changed')
        result=await self._s07_owner_rpc(request.owner,request.workspace_id,payload,asyncio.get_running_loop().time()+6.0)
        ref=request.source
        raw=json.dumps([ref.version,ref.account_id,ref.chat_id,ref.actor_id,ref.message_id,ref.part_id],ensure_ascii=False,separators=(',',':')).encode()
        source_id='hostpart_'+hashlib.sha256(raw).hexdigest()
        if (type(result) is not dict or set(result)!={'version','source_id','state','visibility','covered'}
            or type(result['version']) is not int or result['version']!=1 or result['source_id']!=source_id
            or result['state'] not in ('already_fenced','absent_fenced') or result['visibility']!='closed'
            or not _source_fence_covered(result)):
            raise MemoryRequestRejectedError('reset source outcome requires reconciliation')
        await _queued_owned_thread(self._assert_reset_retirement_ready,closure.lease.reset_transport_witness)
        if await current()!=before or await asyncio.to_thread(self._reset_fence_binding,claim)!=reset:
            raise MemoryRequestRejectedError('reset source proof changed')
        return result

    async def _host_source_fence_inner(self, request, *, rename_operation_id=None, reset_claim=None, mapping=None, guard=None, target_authority=None, reset_closure=None, write_status=None) -> Dict[str, Any]:
        reset_context = None
        if reset_claim is not None:
            from services.memory.nuke_coordinator import MemoryResetClaim
            from src.openclank.session_map import OwnerSessionMap
            from src.openclank.memory_job_admission import TargetAuthority
            if (rename_operation_id is not None or type(reset_claim) is not MemoryResetClaim
                    or type(mapping) is not OwnerSessionMap or type(target_authority) is not TargetAuthority
                    or request.source.account_id != reset_claim.account_id):
                raise MemoryRequestRejectedError('reset source authority unavailable')
            def resolve():
                return (target_authority.resolve(reset_claim.account_id) if reset_closure is None else
                    target_authority._resolve_stopped_reset(reset_claim.account_id,closure=reset_closure,claim=reset_claim))
            binding = resolve()
            def check_reset():
                mapping.validate_chat_guard(request.source.chat_id, guard)
                reset_claim.assert_current(owner=reset_claim.owner, account_id=reset_claim.account_id,
                    provider=self, operation_id=reset_claim.operation_id)
                if (resolve() != binding
                        or binding.memory_owner != request.owner or binding.memory_workspace != request.workspace_id):
                    raise MemoryRequestRejectedError('reset source target changed')
            check_reset.operation_id = reset_claim.operation_id
            check_reset.claim = reset_claim
            check_reset.closure = reset_closure
            check_reset.write_status = write_status
            check_reset()
            reset_context = check_reset
        elif any(value is not None for value in (mapping, guard, target_authority, reset_closure)):
            raise MemoryRequestRejectedError('reset source authority unavailable')
        if type(request) is _HostSourceProjection:
            return await self._host_source_projection(request, fence=True, rename_operation_id=rename_operation_id, reset_context=reset_context)
        from src.memery_external_sources import SourceRef, _source_ref_error
        from src.openclank.memory_job_admission import _conn
        import re
        if (type(request) is not _HostObservationProjection or type(request.source) is not SourceRef or _provider_source_ref_error(self, request.source)
            or not all(type(value) is str and value and value == value.strip() and len(value) <= 512 for value in (request.owner,request.workspace_id,request.origin_id))
            or (rename_operation_id is not None and (type(rename_operation_id) is not str or not rename_operation_id or rename_operation_id != rename_operation_id.strip() or len(rename_operation_id)>512))
            or type(request.captured_owner) is not str or not request.captured_owner or request.captured_owner != request.captured_owner.strip()
            or type(request.origin_hash) is not str or re.fullmatch(r'[0-9a-f]{64}',request.origin_hash) is None
            or type(request.current_fingerprint) is not str or re.fullmatch(r'[0-9a-f]{64}',request.current_fingerprint) is None
            or (request.rebind_id is None) != (request.rebind_hash is None)
            or (request.rebind_id is not None and (type(request.rebind_id) is not str or re.fullmatch(r'cs_s06renew_[0-9a-f]{64}',request.rebind_id) is None or type(request.rebind_hash) is not str or re.fullmatch(r'[0-9a-f]{64}',request.rebind_hash) is None))
            or self._scope(request.owner).owner != request.owner):
            raise MemoryRequestRejectedError('observation source fence unavailable')
        ref = request.source
        encoded = json.dumps([request.captured_owner,request.workspace_id,asdict(ref)],ensure_ascii=False,separators=(',',':')).encode()
        if request.origin_id != 'cs_s06origin_' + hashlib.sha256(encoded).hexdigest():
            raise MemoryRequestRejectedError('observation source identity unavailable')
        try:
            with _conn(self._local_db_path()) as db:
                if not self._s06_source_capability(db) or not self._s06_erase_capability(db):
                    raise ValueError('observation capability unavailable')
                context_admitted = self._s06_context_admitted_capability(db)
                maintenance = self._maintenance_capability(db)
                if context_admitted and self._guarded_preparation.capabilities.get('admitted') is not True:
                    raise ValueError('context admitted capability is inconsistent')
        except (ValueError,TypeError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('observation capability unavailable') from exc
        authority = {'origin_id':request.origin_id,'origin_hash':request.origin_hash,'captured_owner':request.captured_owner,
                     'current_fingerprint':request.current_fingerprint,'rebind_id':request.rebind_id,'rebind_hash':request.rebind_hash}
        payload = {'phase':'observation_reset_fence' if reset_context else 'observation_fence','version':1,'authority':authority,'source':asdict(ref)}
        if rename_operation_id is not None: payload['rename_operation_id'] = rename_operation_id
        if reset_context:
            reset_context()
            payload['reset_operation_id'] = reset_context.operation_id
            payload['reset'] = await asyncio.to_thread(self._reset_fence_binding,reset_context.claim)
        result = await self._host_candidate_request(request.owner,request.workspace_id,payload,require_live=True,
            write_deadline=(float(asyncio.get_running_loop().time()+30.0) if reset_context else None),
            write_status=(reset_context.write_status if reset_context else None),
            reset_feature=(reset_context.closure.lease.reset_transport_witness if reset_context and reset_context.closure is not None else
                reset_context.claim._terminal_s08_witness[0] if reset_context and reset_context.claim._terminal_s08_witness is not None else bool(reset_context)),
            terminal_feature=(reset_context.claim._terminal_s08_witness if reset_context else None))
        if reset_context:
            reset_context()
            if await asyncio.to_thread(self._reset_fence_binding,reset_context.claim)!=payload['reset']:
                raise MemoryRequestRejectedError('reset fence snapshot changed')
        part = json.dumps([ref.version,ref.account_id,ref.chat_id,ref.actor_id,ref.message_id,ref.part_id],ensure_ascii=False,separators=(',',':')).encode()
        source_id = 'hostpart_' + hashlib.sha256(part).hexdigest()
        expected = {'version','source_id','state','visibility','covered'} | ({'rename_proof'} if rename_operation_id is not None else set())
        allowed = {'fenced','already_fenced','absent_fenced'} | ({'rename_absent'} if rename_operation_id is not None else set())
        if (type(result) is not dict or set(result) != expected or type(result['version']) is not int or result['version'] != 1
            or result['source_id'] != source_id or result['state'] not in allowed or result['visibility'] != 'closed'
            or result['covered'] != (['candidate','curated','knowledge','graph','digest','scoped_quality','reindex','feedback']
                + (['admitted'] if self._guarded_preparation.capabilities.get('admitted') is True else [])
                + (['context_admitted'] if context_admitted and self._guarded_preparation.capabilities.get('admitted') is True else [])
                + (['maintenance'] if maintenance else []))):
            raise MemoryRequestRejectedError('observation visibility closure unavailable')
        if rename_operation_id is not None:
            proof = result['rename_proof']
            material = json.dumps([ref.account_id,rename_operation_id,source_id],ensure_ascii=False,separators=(',',':')).encode()
            if (type(proof) is not dict or set(proof) != {'change_set_id','active_versions'} or proof['change_set_id'] != 'cs_s04rename_'+hashlib.sha256(material).hexdigest()
                or type(proof['active_versions']) is not list or len(proof['active_versions']) > 128
                or (result['state'] == 'rename_absent' and proof['active_versions'])):
                raise MemoryRequestRejectedError('observation rename proof unavailable')
            previous = 0
            for item in proof['active_versions']:
                if (type(item) is not dict or set(item) != {'revision','content_hash'} or type(item['revision']) is not int
                    or item['revision'] <= previous or type(item['content_hash']) is not str or re.fullmatch(r'[0-9a-f]{64}',item['content_hash']) is None):
                    raise MemoryRequestRejectedError('observation rename proof unavailable')
                previous = item['revision']
        return result

    async def _host_source_renew(self, request: _HostSourceRenew, *, capability) -> Dict[str, Any]:
        """Renew a previously active source only through the live rename authority."""
        from src.memery_external_sources import SourceRef, _source_ref_error
        from src.openclank.chat_source_bridge import HostAccountSourceTransition
        if (type(request) is not _HostSourceRenew or type(request.source) is not SourceRef
            or _provider_source_ref_error(self, request.source) or type(capability) is not HostAccountSourceTransition
            or not all(type(value) is str and value and value == value.strip() and len(value) <= 512
                       for value in (request.owner,request.workspace_id,request.original_owner,request.operation_id,request.job_id))
            or capability.operation_id != request.operation_id or self._scope(request.owner).owner != request.owner):
            raise MemoryRequestRejectedError('host source renewal unavailable')
        operation = capability.lifecycle.get_operation(request.operation_id)
        if (operation.get('kind') != 'rename' or operation.get('source_owner') != request.original_owner
            or operation.get('target_owner') != request.owner):
            raise MemoryRequestRejectedError('host source renewal scope changed')
        ref = request.source
        identity = json.dumps([ref.version,ref.account_id,ref.chat_id,ref.actor_id,ref.message_id,ref.part_id],
                              ensure_ascii=False,separators=(',',':')).encode('utf-8')
        source_id = 'hostpart_' + hashlib.sha256(identity).hexdigest()
        proof_material = json.dumps([ref.account_id,request.operation_id,source_id],ensure_ascii=False,separators=(',',':')).encode('utf-8')
        if request.rename_proof_id != 'cs_s04rename_' + hashlib.sha256(proof_material).hexdigest():
            raise MemoryRequestRejectedError('host source renewal proof unavailable')
        body = {'version':1,'owner':request.owner,'workspace_id':request.workspace_id,
                'original_owner':request.original_owner,'operation_id':request.operation_id,'job_id':request.job_id,
                'source':asdict(ref),'rename_proof_id':request.rename_proof_id}
        capability.assert_current(provider=self,action='renew',owner=request.owner)
        result = await self._host_candidate_request(request.owner,request.workspace_id,
            {'phase':'source_renew','request':body},require_live=True)
        if (set(result) != {'version','source_id','revision','state','source_state'} or type(result['version']) is not int
            or result['version'] != 1 or result['source_id'] != source_id or type(result['revision']) is not int
            or result['revision'] != ref.revision or result['state'] not in ('renewed','already_applied')
            or result['source_state'] not in ('active','forget_requested','forgotten')
            or (result['state'] == 'renewed' and result['source_state'] != 'active')):
            raise MemoryRequestRejectedError('invalid host source renewal response')
        return result

    async def _host_observation_renew(self, request: _HostObservationRenew, *, capability, target_authority) -> Dict[str, Any]:
        """Rebind one immutable observation only under live rename and target authority."""
        from src.memery_external_sources import SourceRef, _source_ref_error
        from src.openclank.chat_source_bridge import HostAccountSourceTransition
        from src.openclank.memory_job_admission import TargetAuthority, _conn, _hash
        import re
        if (type(request) is not _HostObservationRenew or type(request.source) is not SourceRef or _provider_source_ref_error(self, request.source)
            or type(capability) is not HostAccountSourceTransition or type(target_authority) is not TargetAuthority
            or not all(type(v) is str and v and v == v.strip() and len(v) <= 512 for v in (request.owner,request.workspace_id,request.original_owner,request.operation_id,request.captured_owner))
            or not _hash(request.origin_hash) or not _hash(request.previous_fingerprint) or not _hash(request.current_fingerprint)
            or (request.prior_receipt_id is None) != (request.prior_receipt_hash is None)
            or (request.prior_receipt_id is None and request.original_owner != request.captured_owner)
            or (request.prior_receipt_id is not None and (type(request.prior_receipt_id) is not str or not re.fullmatch(r'cs_s06renew_[0-9a-f]{64}',request.prior_receipt_id) or not _hash(request.prior_receipt_hash)))
            or capability.operation_id != request.operation_id or self._scope(request.owner).owner != request.owner):
            raise MemoryRequestRejectedError('observation renewal unavailable')
        operation = capability.lifecycle.get_operation(request.operation_id)
        target = target_authority.resolve(request.source.account_id)
        if (operation.get('kind') != 'rename' or operation.get('source_owner') != request.original_owner
            or operation.get('target_owner') != request.owner or target.memory_owner != request.owner
            or target.memory_workspace != request.workspace_id or target.fingerprint != request.current_fingerprint):
            raise MemoryRequestRejectedError('observation renewal scope changed')
        ref = request.source
        origin_bytes = json.dumps([request.captured_owner,request.workspace_id,asdict(ref)],ensure_ascii=False,separators=(',',':')).encode()
        source_bytes = json.dumps([ref.version,ref.account_id,ref.chat_id,ref.actor_id,ref.message_id,ref.part_id],ensure_ascii=False,separators=(',',':')).encode()
        source_id = 'hostpart_'+hashlib.sha256(source_bytes).hexdigest()
        fence_bytes = json.dumps([ref.account_id,request.operation_id,source_id],ensure_ascii=False,separators=(',',':')).encode()
        if request.origin_id != 'cs_s06origin_'+hashlib.sha256(origin_bytes).hexdigest() or request.rename_proof_id != 'cs_s04rename_'+hashlib.sha256(fence_bytes).hexdigest():
            raise MemoryRequestRejectedError('observation renewal identity unavailable')
        try:
            with _conn(self._local_db_path()) as db:
                if not self._s06_source_capability(db): raise ValueError('observation capability unavailable')
        except (ValueError,TypeError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('observation capability unavailable') from exc
        body = {'version':2, **asdict(request)}
        capability.assert_current(provider=self,action='renew',owner=request.owner)
        if target_authority.resolve(ref.account_id) != target: raise MemoryRequestRejectedError('observation renewal target changed')
        result = await self._host_candidate_request(request.owner,request.workspace_id,{'phase':'observation_renew','request':body},require_live=True)
        receipt_material = json.dumps([ref.account_id,request.operation_id,source_id,ref.revision,ref.content_hash],ensure_ascii=False,separators=(',',':')).encode()
        if (type(result) is not dict or set(result) != {'version','source_id','revision','state','source_state','receipt_id','receipt_sha256'}
            or type(result['version']) is not int or result['version'] != 2 or result['source_id'] != source_id
            or type(result['revision']) is not int or result['revision'] != ref.revision or result['state'] not in ('renewed','already_applied')
            or result['source_state'] not in ('active','forget_requested','forgotten') or (result['state'] == 'renewed' and result['source_state'] != 'active')
            or result['receipt_id'] != 'cs_s06renew_'+hashlib.sha256(receipt_material).hexdigest() or not _hash(result['receipt_sha256'])):
            raise MemoryRequestRejectedError('observation renewal receipt unavailable')
        expected_receipt = {'version':'s06.observation-rebind.v2','origin_id':request.origin_id,'origin_hash':request.origin_hash,
            'captured_owner':request.captured_owner,'prior_receipt_id':request.prior_receipt_id,'prior_receipt_hash':request.prior_receipt_hash,
            'previous_owner':request.original_owner,'owner':request.owner,'workspace_id':request.workspace_id,'account_id':ref.account_id,
            'source_id':source_id,'revision':ref.revision,'content_hash':ref.content_hash,'operation_id':request.operation_id,
            'fence_proof_id':request.rename_proof_id,'previous_fingerprint':request.previous_fingerprint,'current_fingerprint':request.current_fingerprint}
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value: raise ValueError('duplicate receipt key')
                value[key] = item
            return value
        try:
            with _conn(self._local_db_path()) as db:
                row = db.execute("SELECT content_hash,CASE WHEN length(operation_metadata_json)<=16384 THEN operation_metadata_json END AS payload FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s06.observation-rebind.v2'",(request.owner,result['receipt_id'])).fetchone()
                out = db.execute("SELECT CASE WHEN length(payload_json)<=16384 THEN payload_json END AS payload,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='s06_observation_rebound'",(request.owner,'outbox_'+result['receipt_id']+'_s06_observation_rebound',result['receipt_id'])).fetchone()
                if (not row or type(row['payload']) is not str or row['content_hash'] != result['receipt_sha256']
                    or hashlib.sha256(row['payload'].encode()).hexdigest() != result['receipt_sha256']
                    or json.loads(row['payload'],object_pairs_hook=unique) != expected_receipt or not out or type(out['payload']) is not str
                    or json.loads(out['payload'],object_pairs_hook=unique) != expected_receipt or out['source_revision'] != ref.revision):
                    raise ValueError('observation renewal receipt changed')
        except (ValueError,TypeError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('observation renewal receipt unavailable') from exc
        return result

    async def _host_source_candidate(self, request: _HostSourceCandidate, *, before_issue) -> Dict[str, Any]:
        """Publish an inferred proposal through the existing live private transport."""
        from src.memery_external_sources import SourceRef
        import re
        if (type(request) is not _HostSourceCandidate or type(request.projection) is not _HostSourceProjection
            or type(request.projection.source) is not SourceRef or _provider_source_ref_error(self, request.projection.source) or type(request.attempt_id) is not str
            or not request.attempt_id or request.attempt_id != request.attempt_id.strip() or len(request.attempt_id) > 512
            or type(request.lease_epoch) is not int or request.lease_epoch < 1
            or type(request.content) is not str or not request.content or request.content != request.content.strip()
            or len(request.content.encode('utf-8')) > 4096):
            raise MemoryRequestRejectedError('host source candidate unavailable')
        projection = request.projection
        if (not all(type(value) is str and value and value == value.strip() and len(value) <= 512
                    for value in (projection.owner, projection.workspace_id, projection.job_id))
            or re.fullmatch(r'[0-9a-f]{64}', projection.policy_fingerprint or '') is None
            or self._scope(projection.owner).owner != projection.owner):
            raise MemoryRequestRejectedError('host source candidate scope unavailable')
        body = {'version':1, 'owner':projection.owner, 'workspace_id':projection.workspace_id,
                'job_id':projection.job_id, 'attempt_id':request.attempt_id, 'lease_epoch':request.lease_epoch,
                'source':asdict(projection.source), 'policy_fingerprint':projection.policy_fingerprint,
                'content':request.content}
        if not callable(before_issue): raise MemoryRequestRejectedError('source publication authority unavailable')
        if self._paired_review_ticket is not None: raise MemoryRequestRejectedError('source publication outcome requires original status')
        write_status={}
        result = await self._host_candidate_request(projection.owner, projection.workspace_id,
            {'phase':'source_candidate', 'request':body}, require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,
            before_issue=before_issue,write_status=write_status)
        identity = json.dumps([projection.source.account_id,projection.job_id], ensure_ascii=False,
                              separators=(',',':')).encode('utf-8')
        expected = 's05candidate_' + hashlib.sha256(identity).hexdigest()
        if (set(result) != {'version','candidate_id'} or type(result['version']) is not int
            or result['version'] != 1 or result['candidate_id'] != expected):
            raise MemoryRequestRejectedError('invalid host source candidate response')
        ticket=write_status.get('ticket')
        if ticket is None or self._paired_review_ticket is not ticket or ticket.get('pending_pair_io_settled') is not True:
            raise MemoryRequestRejectedError('source publication custody changed')
        ticket['source_publication_fact']={'version':1,'state':'applied','candidate_id':result['candidate_id']}
        return result

    async def _host_pair_source_candidate(self, request: _HostPairedSourceCandidate, *, before_issue) -> Dict[str, Any]:
        """Private staged proposal; caller holds current pair/target/attempt authority."""
        from src.memery_external_sources import AssistantInterpretationRef, _assistant_ref_error
        if (type(request) is not _HostPairedSourceCandidate or type(request.projection) is not _HostPairedSourceProjection
            or type(request.projection.source) is not AssistantInterpretationRef or _assistant_ref_error(request.projection.source)
            or _provider_source_ref_error(self, request.projection.source.root)
            or type(request.attempt_id) is not str or not request.attempt_id or request.attempt_id!=request.attempt_id.strip() or len(request.attempt_id)>512
            or type(request.lease_epoch) is not int or request.lease_epoch<1 or type(request.content) is not str
            or not request.content or request.content!=request.content.strip() or len(request.content.encode('utf-8'))>4096):
            raise MemoryRequestRejectedError('paired source candidate unavailable')
        projection=request.projection; ref=projection.source
        if (not all(type(v) is str and v and v==v.strip() and len(v)<=512 for v in (projection.owner,projection.workspace_id,projection.job_id))
            or re.fullmatch(r'[0-9a-f]{64}',projection.policy_fingerprint or '') is None or self._scope(projection.owner).owner!=projection.owner):
            raise MemoryRequestRejectedError('paired source candidate scope unavailable')
        coverage={'version':1,'kind':'assistant_interpretation_review','assistant':{'message_id':ref.assistant_message_id,
            'part_id':ref.assistant_part_id,'revision':ref.assistant_revision,'content_hash':ref.assistant_content_hash},'pair_sha256':ref.pair_sha256}
        body={'version':1,'owner':projection.owner,'workspace_id':projection.workspace_id,'job_id':projection.job_id,
            'attempt_id':request.attempt_id,'lease_epoch':request.lease_epoch,'source':asdict(ref.root),'coverage':coverage,
            'policy_fingerprint':projection.policy_fingerprint,'content':request.content}
        if not callable(before_issue):raise MemoryRequestRejectedError('paired publication authority unavailable')
        if self._paired_review_ticket is not None:raise MemoryRequestRejectedError('paired publication outcome requires original status')
        write_status={}
        result=await self._host_candidate_request(projection.owner,projection.workspace_id,{'phase':'paired_source_candidate','request':body},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue,write_status=write_status)
        digest=hashlib.sha256(json.dumps([ref.root.account_id,projection.job_id],ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        if result!={'version':1,'candidate_id':'s05paircandidate_'+digest} or type(result.get('version')) is not int:
            raise MemoryRequestRejectedError('paired candidate receipt unavailable')
        ticket=write_status.get('ticket')
        if ticket is None or self._paired_review_ticket is not ticket or ticket.get('pending_pair_io_settled') is not True:
            raise MemoryRequestRejectedError('paired publication custody changed')
        ticket['paired_publication_fact']={'version':1,'state':'applied','candidate_id':result['candidate_id'],'visibility':'not_rechecked'}
        return result

    def _parse_pair_review_item(self, item):
        from src.memery_external_sources import SourceRef, AssistantInterpretationRef, _assistant_ref_error
        if (type(item) is not dict or set(item)!={'version','kind','candidate_id','candidate_digest','association_sha256','proposal','source','coverage'}
            or type(item['version']) is not int or item['version']!=1 or item['kind']!='assistant_interpretation'
            or type(item.get('candidate_id')) is not str or re.fullmatch(r's05paircandidate_[0-9a-f]{64}',item['candidate_id']) is None
            or any(type(item.get(k)) is not str or re.fullmatch(r'[0-9a-f]{64}',item[k]) is None for k in ('candidate_digest','association_sha256'))
            or type(item['proposal']) is not str or not item['proposal'].strip() or len(item['proposal'].encode('utf-8'))>4096):
            raise MemoryRequestRejectedError('paired review unavailable')
        try:
            source=SourceRef(**item['source']); coverage=item['coverage']; part=coverage['assistant']
            if (type(coverage) is not dict or set(coverage)!={'version','kind','assistant','pair_sha256'}
                or type(coverage['version']) is not int or coverage['version']!=1 or coverage['kind']!='assistant_interpretation_review'
                or type(part) is not dict or set(part)!={'message_id','part_id','revision','content_hash'}):
                raise ValueError('invalid paired coverage')
            ref=AssistantInterpretationRef(1,source,part['message_id'],part['part_id'],part['revision'],part['content_hash'],coverage['pair_sha256'])
            if _assistant_ref_error(ref) or _provider_source_ref_error(self, ref.root): raise ValueError('invalid paired source')
        except (KeyError,TypeError,ValueError) as exc:
            raise MemoryRequestRejectedError('paired review unavailable') from exc
        return {'candidate_id':item['candidate_id'],'candidate_digest':item['candidate_digest'],
            'association_sha256':item['association_sha256'],'proposal':item['proposal'],'source':ref}

    async def _host_pair_review_page(self, *, owner, workspace_id, after=None, limit=32, deadline=None):
        if (type(owner) is not str or not owner or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id
            or type(limit) is not int or not 1<=limit<=32 or (after is not None and (type(after) is not str or re.fullmatch(r's05paircandidate_[0-9a-f]{64}',after) is None))):
            raise MemoryRequestRejectedError('paired review page unavailable')
        payload={'phase':'paired_review_page','after':after,'limit':limit}
        if deadline is None:
            result=await self._host_candidate_request(owner,workspace_id,payload,require_live=True)
        else:
            import math
            if type(deadline) not in (int,float) or not math.isfinite(deadline): raise MemoryRequestRejectedError('paired review page unavailable')
            result=await self._s07_owner_rpc(owner,workspace_id,payload,deadline,read_ticket={})
        if (type(result) is not dict or set(result)!={'version','items','next_cursor','partial'} or type(result['version']) is not int or result['version']!=1
            or type(result['items']) is not list or len(result['items'])>limit or type(result['partial']) is not bool
            or (result['next_cursor'] is not None and (type(result['next_cursor']) is not str or re.fullmatch(r's05paircandidate_[0-9a-f]{64}',result['next_cursor']) is None))):
            raise MemoryRequestRejectedError('paired review page unavailable')
        items=[self._parse_pair_review_item(item) for item in result['items']]
        cursor=result['next_cursor']
        if (any(items[i]['candidate_id']>=items[i+1]['candidate_id'] for i in range(len(items)-1))
            or (after is not None and (any(item['candidate_id']<=after for item in items) or (cursor is not None and cursor<=after)))
            or (cursor is not None and any(item['candidate_id']>cursor for item in items))):
            raise MemoryRequestRejectedError('paired review page changed')
        return {'items':items,'next_cursor':result['next_cursor'],'partial':result['partial']}

    async def _host_pair_review_preview(self, request: _HostCandidatePreview):
        if (type(request) is not _HostCandidatePreview or type(request.candidate_id) is not str or re.fullmatch(r's05paircandidate_[0-9a-f]{64}',request.candidate_id) is None
            or type(request.owner) is not str or not request.owner or self._scope(request.owner).owner!=request.owner or request.workspace_id!=self._workspace_id):
            raise MemoryRequestRejectedError('paired review unavailable')
        result=await self._host_candidate_request(request.owner,request.workspace_id,
            {'phase':'paired_review_preview','candidate_id':request.candidate_id},require_live=True)
        item=self._parse_pair_review_item(result)
        if item['candidate_id']!=request.candidate_id:
            raise MemoryRequestRejectedError('paired review changed')
        return item

    @staticmethod
    def _pair_review_body(request, policy=None):
        if (type(request) is not _HostPairedReview or not all(type(v) is str and v and v==v.strip() and len(v)<=512
                for v in (request.owner,request.workspace_id,request.account_id,request.request_key))
            or type(request.candidate_id) is not str or re.fullmatch(r's05paircandidate_[0-9a-f]{64}',request.candidate_id) is None
            or any(type(v) is not str or re.fullmatch(r'[0-9a-f]{64}',v) is None for v in (request.candidate_digest,request.association_sha256))
            or type(request.reason) is not str or not request.reason or request.reason!=request.reason.strip() or len(request.reason.encode('utf-8'))>500
            or request.action not in ('accept','reject')):
            raise MemoryRequestRejectedError('paired review unavailable')
        return {'version':1,'owner':request.owner,'workspace_id':request.workspace_id,'account_id':request.account_id,
            'candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,'association_sha256':request.association_sha256,
            'request_key':request.request_key,'reason':request.reason,'action':request.action,'policy_projection':policy}

    @staticmethod
    def _validate_pair_review_receipt(request, result, *, status):
        if result=={'state':'unknown'}: return result
        if (type(result) is not dict or set(result)!={'state','candidate_id','block_id','curated_id','revision','visibility'}
            or result['candidate_id']!=request.candidate_id or result['visibility'] not in (('not_rechecked',) if status else ('current','not_rechecked'))):
            raise MemoryRequestRejectedError('paired review receipt unavailable')
        if request.action=='reject':
            if result['state']!='rejected' or any(result[k] is not None for k in ('block_id','curated_id','revision')):
                raise MemoryRequestRejectedError('paired review receipt unavailable')
        else:
            digest=hashlib.sha256(json.dumps(request.candidate_id,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
            if (result['state']!='applied' or result['block_id']!='pairblock_'+digest or result['curated_id']!='paircurated_'+digest
                or type(result['revision']) is not int or result['revision']!=1):
                raise MemoryRequestRejectedError('paired review receipt unavailable')
        return result

    async def _host_pair_review_status(self, request: _HostPairedReview):
        body=self._pair_review_body(request)
        if self._scope(request.owner).owner!=request.owner or request.workspace_id!=self._workspace_id:
            raise MemoryRequestRejectedError('paired review scope changed')
        result=await self._host_candidate_request(request.owner,request.workspace_id,{'phase':'paired_review_status','request':body},require_live=True)
        result=self._validate_pair_review_receipt(request,result,status=True)
        if result.get('state') in ('applied','rejected'):
            local=self._host_pair_review_inflight(owner=request.owner,workspace_id=request.workspace_id,account_id=request.account_id,candidate_id=request.candidate_id)
            if local is not None and local['original']=={'candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,'association_sha256':request.association_sha256,'request_key':request.request_key,'reason':request.reason,'action':request.action}:
                self._paired_review_ticket=None
        return result

    # The native explicit Add transaction has a five-second work budget.
    # Allow transport, final authorization and durable commit to finish as well.
    _EXPLICIT_ADD_TIMEOUT = 8.0

    def _explicit_add_profile_ready(self):
        witness=self._explicit_add_witness
        return bool(type(witness) is tuple and len(witness)==5 and self._host_bounded_write_available()
            and witness[0] is self._owned_transport and witness[1] is self._owned_generation
            and witness[2] is self._owned_transport.process and witness[3] is self._owner_task
            and witness[4] is self._guarded_preparation)

    async def _host_negotiate_explicit_add(self, *, owner, workspace_id):
        if self._explicit_add_profile_ready():
            try:
                await asyncio.to_thread(self._explicit_add_witness[4].assert_valid)
                self._assert_reset_handoff_access()
                return self._explicit_add_profile_ready()
            except Exception:return False
        self._explicit_add_witness=None
        if not self._host_bounded_write_available():return False
        transport=self._owned_transport;generation=self._owned_generation;process=transport.process
        task=self._owner_task;prepared=self._guarded_preparation
        try:
            await asyncio.to_thread(prepared.assert_valid)
            self._assert_reset_handoff_access()
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'explicit_add_capability'},asyncio.get_running_loop().time()+self._EXPLICIT_ADD_TIMEOUT,read_ticket={})
            expected={'version':1,'feature':'explicit-add-import-review-v1','database_id':prepared.database_id,
                'device':prepared.device,'inode':prepared.inode,'schema_cookie':prepared.schema_cookie,
                'operations':['add','add_status','review','review_edit','review_reject','question_answer','import_review','import_status','import_staged_review','missing_original_pages','missing_terminal_pages','reset_terminal']}
            if (type(result) is not dict or any(type(result.get(k)) is not int for k in ('version','device','inode','schema_cookie')) or result!=expected
                    or not self._host_bounded_write_available() or self._owned_transport is not transport
                    or self._owned_generation is not generation or transport.process is not process
                    or self._owner_task is not task or self._guarded_preparation is not prepared):return False
            await asyncio.to_thread(prepared.assert_valid)
            self._assert_reset_handoff_access()
            if (not self._host_bounded_write_available() or self._owned_transport is not transport
                    or self._owned_generation is not generation or transport.process is not process
                    or self._owner_task is not task or self._guarded_preparation is not prepared):return False
            self._explicit_add_witness=(transport,generation,process,task,prepared)
            return self._explicit_add_profile_ready()
        except Exception:return False

    def _native_memory_ops_ready(self):
        witness=self._native_memory_ops_witness
        return bool(type(witness) is tuple and len(witness)==6 and self._host_bounded_write_available()
            and witness[0] is self._owned_transport and witness[1] is self._owned_generation
            and witness[2] is self._owned_transport.process and witness[3] is self._owner_task
            and witness[4] is self._guarded_preparation and witness[4].schema_version==18)

    def _native_memory_ops_support(self):
        if not self._native_memory_ops_ready(): return ()
        return self._native_memory_ops_witness[5]

    async def _host_negotiate_native_memory_ops(self, *, owner, workspace_id, deadline=None, _rank_proof=None):
        loop=asyncio.get_running_loop()
        if deadline is not None and loop.time()>=deadline:return False
        async def validate_preparation():
            if _rank_proof is None:
                check=prepared.assert_valid
            else:
                from src.openclank.memory_policy_projection import _RankPreparationProof,policy_coordinator
                coordinator=policy_coordinator()
                if type(_rank_proof) is not _RankPreparationProof or coordinator is None:
                    raise ValueError('rank preparation unavailable')
                check=lambda:_rank_proof.check(coordinator,owner,_rank_proof.account,workspace_id,self)
            if deadline is None:
                await asyncio.to_thread(check)
            else:
                remaining=deadline-loop.time()
                if remaining<=0:raise TimeoutError('capability deadline')
                await asyncio.wait_for(asyncio.to_thread(check),timeout=remaining)
        prepared=self._guarded_preparation
        if prepared is None or prepared.schema_version!=18 or not self._host_bounded_write_available(): return False
        if self._native_memory_ops_ready() and not any(op in self._native_memory_ops_support() for op in (NATIVE_PREEXTRACTED_OPERATION,NATIVE_UPDATE_BATCH_OPERATION)):
            try:
                await validate_preparation()
                self._assert_reset_handoff_access()
                return self._native_memory_ops_ready()
            except Exception: return False
        # Re-query the optional F8 profile rather than treating its prior marker
        # as permanent; Native checks the admitted profile and schema stamp.
        self._native_memory_ops_witness=None
        transport=self._owned_transport; generation=self._owned_generation; process=transport.process; task=self._owner_task
        try:
            await validate_preparation()
            self._assert_reset_handoff_access()
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'native_memory_ops_capability'},min(deadline,loop.time()+1.5) if deadline is not None else loop.time()+1.5,read_ticket={})
            expected={'version':1,'feature':'native_memory_ops_v1','database_id':prepared.database_id,
                'device':prepared.device,'inode':prepared.inode,'schema_cookie':prepared.schema_cookie,
                'operations':list(NATIVE_MEMORY_OPS)}
            operations=result.get('operations') if type(result) is dict else None
            allowed=(list(NATIVE_MEMORY_OPS), list(NATIVE_MEMORY_OPS)+[NATIVE_PREEXTRACTED_OPERATION], list(NATIVE_MEMORY_OPS)+[NATIVE_UPDATE_BATCH_OPERATION], list(NATIVE_MEMORY_OPS)+[NATIVE_PREEXTRACTED_OPERATION,NATIVE_UPDATE_BATCH_OPERATION],
                list(NATIVE_MEMORY_OPS)+[NATIVE_PREEXTRACTED_OPERATION,NATIVE_HOST_ANCHOR_OPERATION],
                list(NATIVE_MEMORY_OPS)+[NATIVE_PREEXTRACTED_OPERATION,NATIVE_UPDATE_BATCH_OPERATION,NATIVE_HOST_ANCHOR_OPERATION])
            allowed=allowed+tuple(profile+[NATIVE_RANK_LEARNING_OPERATION] for profile in allowed)
            allowed=allowed+tuple(profile+[NATIVE_VECTOR_PROFILE_OPERATION] for profile in allowed if profile and profile[-1]==NATIVE_RANK_LEARNING_OPERATION)
            if type(operations) is not list or operations not in allowed: return False
            expected['operations']=operations
            if (type(result) is not dict or result!=expected or any(type(result.get(k)) is not int for k in ('version','device','inode','schema_cookie'))
                    or not self._host_bounded_write_available() or self._owned_transport is not transport
                    or self._owned_generation is not generation or transport.process is not process
                    or self._owner_task is not task or self._guarded_preparation is not prepared): return False
            await validate_preparation()
            self._assert_reset_handoff_access()
            if (not self._host_bounded_write_available() or self._owned_transport is not transport
                    or self._owned_generation is not generation or transport.process is not process
                    or self._owner_task is not task or self._guarded_preparation is not prepared): return False
            self._native_memory_ops_witness=(transport,generation,process,task,prepared,tuple(operations))
            return self._native_memory_ops_ready()
        except Exception: return False

    def _native_memory_ops_selector(self, value):
        if type(value) is not dict or set(value)!={'version','owner','workspace_id','account_id','policy_projection','candidate_ids'}:
            raise MemoryRequestRejectedError('batch selector unavailable')
        ids=value.get('candidate_ids')
        if type(ids) is not list or not 1<=len(ids)<=100 or any(type(x) is not str for x in ids) or len(set(ids))!=len(ids):
            raise MemoryRequestRejectedError('batch selector unavailable')
        for candidate in ids:
            self._explicit_add_review_selector({**{k:value[k] for k in ('version','owner','workspace_id','account_id','policy_projection')},'candidate_id':candidate})
        return copy.deepcopy(value)

    def _native_memory_ops_original(self, value, *, apply):
        keys={'selector','request_key','preview_sha256'} if apply else {'version','owner','workspace_id','account_id','policy_projection','request_key','preview_sha256'}
        if type(value) is not dict or set(value)!=keys: raise MemoryRequestRejectedError('batch original unavailable')
        if apply: self._native_memory_ops_selector(value['selector'])
        else:
            self._explicit_add_review_selector({**{k:value[k] for k in ('version','owner','workspace_id','account_id','policy_projection')},'candidate_id':'manualadd_'+'0'*64})
        if (type(value.get('request_key')) is not str or re.fullmatch(r'[A-Za-z0-9_-]{22,128}',value['request_key']) is None
                or type(value.get('preview_sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}',value['preview_sha256']) is None):
            raise MemoryRequestRejectedError('batch original unavailable')
        return copy.deepcopy(value)

    @staticmethod
    def _native_memory_ops_result(value, *, original, ids=None, status=False):
        if status and value in ({'state':'unknown'}, {'state':'terminal'}): return value
        if (type(value) is not dict or set(value)!={'state','batch_id','items'} or value['state']!='committed'
                or type(value['batch_id']) is not str or re.fullmatch(r'cs_s01batch_[0-9a-f]{64}',value['batch_id']) is None
                or type(value['items']) is not list or not 1<=len(value['items'])<=100):
            raise MemoryRequestRejectedError('batch result unavailable')
        material=['s01.explicit-add-batch-id.v1',original['account_id'],original['workspace_id'],original['request_key']]
        expected='cs_s01batch_'+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
        if value['batch_id']!=expected: raise MemoryRequestRejectedError('batch result changed')
        for item in value['items']:
            if (type(item) is not dict or set(item)!={'candidate_id','curated_id','block_id','revision'}
                    or type(item['candidate_id']) is not str or re.fullmatch(r'manualadd_[0-9a-f]{64}',item['candidate_id']) is None
                    or any(type(item[k]) is not str or not item[k] or len(item[k])>512 for k in ('curated_id','block_id'))
                    or type(item['revision']) is not int or item['revision']<1): raise MemoryRequestRejectedError('batch result unavailable')
        candidates=[item['candidate_id'] for item in value['items']]
        if len(set(candidates))!=len(candidates) or (ids is not None and candidates!=ids): raise MemoryRequestRejectedError('batch result unavailable')
        return value

    async def _host_native_memory_ops_batch_preview(self, selector):
        body=self._native_memory_ops_selector(selector)
        if not self._native_memory_ops_ready(): raise MemoryRequestRejectedError('batch capability unavailable')
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'native_memory_ops_batch_preview','request':body},asyncio.get_running_loop().time()+5.0,read_ticket={})
        if (type(result) is not dict or set(result)!={'state','items','preview_sha256'} or result['state']!='preview'
                or type(result['preview_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['preview_sha256']) is None
                or type(result['items']) is not list or len(result['items'])!=len(body['candidate_ids'])):
            raise MemoryRequestRejectedError('batch preview unavailable')
        for candidate,item in zip(body['candidate_ids'],result['items']):
            self._explicit_add_review_preview_result(item,{**{k:body[k] for k in ('owner','workspace_id','account_id')},'candidate_id':candidate})
        if not self._native_memory_ops_ready(): raise MemoryRequestRejectedError('batch capability changed')
        return result

    async def _host_native_memory_ops_batch_apply(self, request, *, before_issue):
        body=self._native_memory_ops_original(request,apply=True); selector=body['selector']
        if self._native_memory_ops_ticket is not None or not self._native_memory_ops_ready() or not callable(before_issue):
            raise MemoryRequestRejectedError('batch outcome requires status')
        await asyncio.to_thread(self._guarded_preparation.assert_valid)
        if not self._native_memory_ops_ready(): raise MemoryRequestRejectedError('batch capability changed')
        identity={**{k:selector[k] for k in ('version','owner','workspace_id','account_id','policy_projection')},
            'request_key':body['request_key'],'preview_sha256':body['preview_sha256']}
        prepared=self._guarded_preparation
        original={'identity':identity,'ids':selector['candidate_ids'],'transport':self._owned_transport,'generation':self._owned_generation,
            'process':self._owned_transport.process,'database':(prepared.database_path,prepared.database_id,prepared.device,prepared.inode,prepared.schema_version,prepared._schema_fingerprint)}
        self._native_memory_ops_ticket=original
        write_status={}
        try:
            def issue_current():
                return self._native_memory_ops_ready() and before_issue() is True
            value=await self._host_candidate_request(selector['owner'],selector['workspace_id'],{'phase':'native_memory_ops_batch_apply','request':body},
                require_live=True,write_deadline=asyncio.get_running_loop().time()+5.0,before_issue=issue_current,write_status=write_status)
            result=self._native_memory_ops_result(value,original=identity,ids=selector['candidate_ids'])
            if self._native_memory_ops_ticket is original and self._native_memory_ops_ready(): self._native_memory_ops_ticket=None
            return result
        except BaseException:
            ticket=write_status.get('ticket')
            if self._native_memory_ops_ticket is original and (ticket is None or (ticket.get('issued') is False and ticket.get('owned_io_drained') is True)):
                self._native_memory_ops_ticket=None
            raise

    async def _host_native_memory_ops_batch_status(self, request):
        body=self._native_memory_ops_original(request,apply=False); ticket=self._native_memory_ops_ticket
        if ticket is not None and ticket.get('identity')!=body: raise MemoryRequestRejectedError('batch original changed')
        if not self._native_memory_ops_ready(): raise MemoryRequestRejectedError('batch capability unavailable')
        read_ticket={}
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'native_memory_ops_batch_status','request':body},asyncio.get_running_loop().time()+5.0,read_ticket=read_ticket)
        result=self._native_memory_ops_result(result,original=body,ids=ticket.get('ids') if ticket else None,status=True)
        if result['state'] in ('committed','terminal') and ticket is not None and self._native_memory_ops_ticket is ticket:
            transport=self._owned_transport; generation=self._owned_generation; prepared=self._guarded_preparation
            same=ticket['transport'] is transport and ticket['generation'] is generation
            if not same:
                old=ticket['transport']; process=ticket['process']
                try:
                    exited=old.exit_confirmation(ticket['generation'])
                    same=(old.process is process and process.returncode is not None and exited.done()
                        and not exited.cancelled() and exited.result()==process.returncode
                        and read_ticket.get('transport') is transport and read_ticket.get('generation') is generation
                        and prepared is not None and ticket['database']==(prepared.database_path,prepared.database_id,prepared.device,prepared.inode,prepared.schema_version,prepared._schema_fingerprint))
                    if same: await asyncio.to_thread(prepared.assert_valid)
                except Exception: same=False
            if (same and self._native_memory_ops_ticket is ticket and self._owned_transport is transport
                    and self._owned_generation is generation and self._guarded_preparation is prepared and self._native_memory_ops_ready()):
                self._native_memory_ops_ticket=None
        return result

    def _native_ops_flat_scope(self, value, extra):
        base={'version','owner','workspace_id','account_id','policy_projection'}
        if (type(value) is not dict or set(value)!=base|set(extra) or value.get('version')!=1
                or any(type(value[k]) is not str or not value[k] or value[k]!=value[k].strip() or len(value[k])>512 for k in ('owner','workspace_id','account_id'))
                or type(value['policy_projection']) is not dict or self._scope(value['owner']).owner!=value['owner']
                or value['workspace_id']!=self._workspace_id):
            raise MemoryRequestRejectedError('native memory op scope unavailable')
        return copy.deepcopy(value)

    async def _host_preextracted_host_anchor_stage(self, admission):
        from src.openclank.m1_inferred_proposal import _project_host_anchor_admission
        body, before_issue = _project_host_anchor_admission(admission, self)
        body=self._native_ops_flat_scope(body,{'request_key','items'})
        if (NATIVE_HOST_ANCHOR_OPERATION not in self._native_memory_ops_support() or not callable(before_issue)
                or re.fullmatch(r'm1op_[0-9a-f]{64}',str(body['request_key'])) is None
                or type(body['items']) is not list or len(body['items'])!=1):
            raise MemoryRequestRejectedError('host anchor capability unavailable')
        await asyncio.to_thread(self._guarded_preparation.assert_valid)
        value=await self._host_candidate_request(body['owner'],body['workspace_id'],{'phase':'preextracted_host_anchor_stage','request':body},
            require_live=True,write_deadline=asyncio.get_running_loop().time()+5.0,before_issue=before_issue,write_status={})
        return self._host_validate_anchor_stage_result(value)

    @staticmethod
    def _host_validate_anchor_stage_result(value):
        if (type(value) is not dict or set(value)!={'version','items'} or type(value['version']) is not int or value['version']!=1
                or type(value['items']) is not list or len(value['items'])!=1):
            raise MemoryRequestRejectedError('host anchor result unavailable')
        item=value['items'][0]
        if (type(item) is not dict or not {'candidate_id','state'}<=set(item) or not set(item)<={'candidate_id','state','conflict_id'}
                or re.fullmatch(r'preextracted_[0-9a-f]{64}',str(item['candidate_id'])) is None
                or item['state'] not in ('staged','duplicate','conflict')
                or ('conflict_id' in item)!=(item['state']=='conflict')
                or (item['state']=='conflict' and re.fullmatch(r'preextracted_conflict_[0-9a-f]{64}',str(item['conflict_id'])) is None)):
            raise MemoryRequestRejectedError('host anchor result unavailable')
        return value

    async def _host_preextracted_host_anchor_status(self, request):
        body=self._native_ops_flat_scope(request,{'request_key'})
        if (NATIVE_HOST_ANCHOR_OPERATION not in self._native_memory_ops_support()
                or re.fullmatch(r'm1op_[0-9a-f]{64}',str(body['request_key'])) is None):
            raise MemoryRequestRejectedError('host anchor capability unavailable')
        value=await self._host_candidate_request(body['owner'],body['workspace_id'],{'phase':'preextracted_host_anchor_status','request':body})
        if type(value) is not dict: raise MemoryRequestRejectedError('host anchor status unavailable')
        if value.get('state') in ('unknown','terminal') and set(value)=={'state'}: return value
        if (value.get('state')=='committed' and set(value)=={'state','body_sha256','result'}
                and type(value['body_sha256']) is str and re.fullmatch(r'[0-9a-f]{64}',value['body_sha256']) is not None):
            self._host_validate_anchor_stage_result(value['result'])
            return value
        raise MemoryRequestRejectedError('host anchor status unavailable')

    async def _host_preextracted_stage(self, request, *, before_issue):
        """Stage ready-made typed facts as pending inferred candidates; Native owns provenance and idempotency."""
        from src.openclank.memory_batch_wire import WireError, stage_request_key, validate_preextracted_items
        body=self._native_ops_flat_scope(request,{'request_key','items'})
        try: stage_request_key(body['request_key']); validate_preextracted_items(body['items'])
        except WireError as exc: raise MemoryRequestRejectedError('pre-extracted request unavailable') from exc
        if NATIVE_PREEXTRACTED_OPERATION not in self._native_memory_ops_support() or not callable(before_issue): raise MemoryRequestRejectedError('pre-extracted capability unavailable')
        await asyncio.to_thread(self._guarded_preparation.assert_valid)
        value=await self._host_candidate_request(body['owner'],body['workspace_id'],{'phase':'preextracted_stage','request':body},
            require_live=True,write_deadline=asyncio.get_running_loop().time()+5.0,before_issue=before_issue,write_status={})
        if (type(value) is not dict or set(value)!={'version','items'} or value['version']!=1 or type(value['items']) is not list
                or len(value['items'])!=len(body['items'])):
            raise MemoryRequestRejectedError('pre-extracted result unavailable')
        for item in value['items']:
            if (type(item) is not dict or not {'candidate_id','state'}<=set(item) or not set(item)<={'candidate_id','state','conflict_id'}
                    or re.fullmatch(r'preextracted_[0-9a-f]{64}',str(item['candidate_id'])) is None
                    or item['state'] not in ('staged','duplicate','conflict')
                    or ('conflict_id' in item)!=(item['state']=='conflict')
                    or (item['state']=='conflict' and re.fullmatch(r'preextracted_conflict_[0-9a-f]{64}',str(item['conflict_id'])) is None)):
                raise MemoryRequestRejectedError('pre-extracted result unavailable')
        return value

    async def _host_preextracted_read(self, phase, request, extra):
        """List or preview pending pre-extracted candidates (read-only; foreign ids answer the same refusal as unknown)."""
        from src.openclank.memory_batch_wire import WireError, candidate_id
        body=self._native_ops_flat_scope(request,set(extra))
        try:
            if phase=='preextracted_review_preview': candidate_id(body['candidate_id'])
            else:
                if body['after'] is not None: candidate_id(body['after'])
                if type(body['limit']) is not int or not 1<=body['limit']<=50: raise WireError('invalid limit')
        except WireError as exc: raise MemoryRequestRejectedError('pre-extracted request unavailable') from exc
        if NATIVE_PREEXTRACTED_OPERATION not in self._native_memory_ops_support(): raise MemoryRequestRejectedError('pre-extracted capability unavailable')
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':phase,'request':body},asyncio.get_running_loop().time()+5.0,read_ticket={})
        if type(result) is not dict or result.get('version')!=1: raise MemoryRequestRejectedError('pre-extracted result unavailable')
        if NATIVE_PREEXTRACTED_OPERATION not in self._native_memory_ops_support(): raise MemoryRequestRejectedError('pre-extracted capability changed')
        return result

    async def _host_preextracted_list(self, request):
        result=await self._host_preextracted_read('preextracted_list',request,{'after','limit'})
        if set(result)!={'version','items','next'} or type(result['items']) is not list:
            raise MemoryRequestRejectedError('pre-extracted result unavailable')
        return result

    async def _host_preextracted_review_preview(self, request):
        result=await self._host_preextracted_read('preextracted_review_preview',request,{'candidate_id'})
        if result.get('state')!='preview' or result.get('candidate_id')!=request['candidate_id'] or result.get('evidence_role')!='inferred':
            raise MemoryRequestRejectedError('pre-extracted result unavailable')
        return result

    async def _host_preextracted_review(self, request, *, before_issue):
        """Named-human accept/reject of one staged candidate; the Host route has verified current write authority."""
        from src.openclank.memory_batch_wire import WireError, candidate_id
        body=self._native_ops_flat_scope(request,{'candidate_id','candidate_digest','action','request_key','reason'})
        try:
            candidate_id(body['candidate_id'])
            if (type(body['candidate_digest']) is not str or re.fullmatch(r'[0-9a-f]{64}',body['candidate_digest']) is None
                    or body['action'] not in ('accept','reject') or type(body['request_key']) is not str or not 1<=len(body['request_key'])<=512
                    or body['request_key']!=body['request_key'].strip() or type(body['reason']) is not str or not 1<=len(body['reason'])<=500):
                raise WireError('invalid review')
        except WireError as exc: raise MemoryRequestRejectedError('pre-extracted review unavailable') from exc
        if NATIVE_PREEXTRACTED_OPERATION not in self._native_memory_ops_support() or not callable(before_issue): raise MemoryRequestRejectedError('pre-extracted capability unavailable')
        await asyncio.to_thread(self._guarded_preparation.assert_valid)
        value=await self._host_candidate_request(body['owner'],body['workspace_id'],{'phase':'preextracted_review','request':body},
            require_live=True,write_deadline=asyncio.get_running_loop().time()+5.0,before_issue=before_issue,write_status={})
        ok=(type(value) is dict and value.get('version')==1 and value.get('candidate_id')==body['candidate_id'])
        if ok and body['action']=='accept':
            ok=(set(value)=={'version','state','candidate_id','curated_id','applied_revision'} and value['state']=='applied'
                and type(value['curated_id']) is str and bool(value['curated_id']) and type(value['applied_revision']) is int and value['applied_revision']>=1)
        elif ok: ok=set(value)=={'version','state','candidate_id'} and value['state']=='rejected'
        if not ok: raise MemoryRequestRejectedError('pre-extracted result unavailable')
        return value

    async def _host_journal_delta_prepare(self, original, *, before_issue):
        """Stage one journal selection through the existing S08 ``s08_model_prepare`` phase (idempotent by its key; no dispatch)."""
        selection=original.get('selection') if type(original) is dict else None
        journal=selection.get('journal') if type(selection) is dict else None
        if (type(journal) is not dict or original.get('output_kind')!='journal_synthesis' or original.get('mode') not in ('model','hybrid')
                or selection.get('candidates')!=[] or selection.get('admissions')!=[] or not callable(before_issue)
                or selection.get('idempotency_key')!='s08_'+str(journal.get('selection_sha256'))
                or self._scope(selection.get('owner')).owner!=selection.get('owner') or selection.get('workspace_id')!=self._workspace_id
                or not self._host_s08_policy_ready('journal_synthesis')):
            raise MemoryRequestRejectedError('journal selection unavailable')
        value=await self._host_candidate_request(selection['owner'],selection['workspace_id'],{'phase':'s08_model_prepare','request':original},
            require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),before_issue=before_issue,write_status={})
        if type(value) is not dict or set(value)!={'version','job_id'} or value['version']!=1 or re.fullmatch(r's08job_[0-9a-f]{64}',str(value['job_id'])) is None:
            raise MemoryRequestRejectedError('journal selection outcome unavailable')
        return value

    async def _host_journal_delta_status(self, request):
        """Read-only original status of one journal selection (never coverage by itself; the archive ACK still re-reads the page)."""
        keys={'version','owner','workspace_id','account_id','policy_projection','selection_sha256','prepare'}
        if (type(request) is not dict or set(request)!=keys or request['version']!=1 or type(request['prepare']) is not dict
                or re.fullmatch(r'[0-9a-f]{64}',str(request['selection_sha256'])) is None
                or self._scope(request['owner']).owner!=request['owner'] or request['workspace_id']!=self._workspace_id
                or not self._host_s08_policy_ready('journal_synthesis')):
            raise MemoryRequestRejectedError('journal status unavailable')
        result=await self._s07_owner_rpc(request['owner'],request['workspace_id'],{'phase':'journal_delta_status','request':request},
            asyncio.get_running_loop().time()+5.0,read_ticket={})
        states=('absent','selected','recorded_proposal','recorded_no_finding','terminal_failed','cancelled','unknown')
        if (type(result) is not dict or set(result)!={'version','state','selection_sha256','page_sha256','prior_cursor','end_seq','job_id','candidate_id','receipt_ids'}
                or result['version']!=1 or result['state'] not in states or result['selection_sha256']!=request['selection_sha256']
                or type(result['receipt_ids']) is not list):
            raise MemoryRequestRejectedError('journal status unavailable')
        return result

    async def _host_update_batch_preview(self, request):
        from src.openclank.memory_batch_wire import WireError, validate_update_items
        body=self._native_ops_flat_scope(request,{'items'})
        try: validate_update_items(body['items'])
        except WireError as exc: raise MemoryRequestRejectedError('update batch unavailable') from exc
        if NATIVE_UPDATE_BATCH_OPERATION not in self._native_memory_ops_support(): raise MemoryRequestRejectedError('update batch capability unavailable')
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'knowledge_update_batch_preview','request':body},asyncio.get_running_loop().time()+5.0,read_ticket={})
        if (type(result) is not dict or set(result)!={'state','items','preview_sha256'} or result['state']!='preview'
                or type(result['preview_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['preview_sha256']) is None
                or type(result['items']) is not list or [i.get('memory_id') if type(i) is dict else None for i in result['items']]!=[i['memory_id'] for i in body['items']]):
            raise MemoryRequestRejectedError('update batch preview unavailable')
        if NATIVE_UPDATE_BATCH_OPERATION not in self._native_memory_ops_support(): raise MemoryRequestRejectedError('update batch capability changed')
        return result

    @staticmethod
    def _update_batch_result(value, ids, *, status=False):
        if status and value in ({'state':'unknown'},{'state':'terminal'}): return value
        if (type(value) is not dict or set(value)!={'state','batch_id','items'} or value['state']!='committed'
                or type(value['batch_id']) is not str or not value['batch_id'] or type(value['items']) is not list
                or [i.get('memory_id') if type(i) is dict else None for i in value['items']]!=ids
                or any(type(i.get('revision')) is not int or i['revision']<2 for i in value['items'])):
            raise MemoryRequestRejectedError('update batch result unavailable')
        return value

    async def _host_update_batch_apply(self, request, *, before_issue):
        from src.openclank.memory_batch_wire import WireError, preview_sha, request_key, validate_update_items
        body=self._native_ops_flat_scope(request,{'request_key','preview_sha256','items'})
        try: request_key(body['request_key']); preview_sha(body['preview_sha256']); validate_update_items(body['items'])
        except WireError as exc: raise MemoryRequestRejectedError('update batch unavailable') from exc
        if NATIVE_UPDATE_BATCH_OPERATION not in self._native_memory_ops_support() or not callable(before_issue): raise MemoryRequestRejectedError('update batch capability unavailable')
        await asyncio.to_thread(self._guarded_preparation.assert_valid)
        value=await self._host_candidate_request(body['owner'],body['workspace_id'],{'phase':'knowledge_update_batch_apply','request':body},
            require_live=True,write_deadline=asyncio.get_running_loop().time()+5.0,before_issue=before_issue,write_status={})
        return self._update_batch_result(value,[i['memory_id'] for i in body['items']])

    async def _host_update_batch_status(self, request):
        from src.openclank.memory_batch_wire import WireError, preview_sha, request_key
        body=self._native_ops_flat_scope(request,{'request_key','preview_sha256'})
        try: request_key(body['request_key']); preview_sha(body['preview_sha256'])
        except WireError as exc: raise MemoryRequestRejectedError('update batch unavailable') from exc
        if NATIVE_UPDATE_BATCH_OPERATION not in self._native_memory_ops_support(): raise MemoryRequestRejectedError('update batch capability unavailable')
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'knowledge_update_batch_status','request':body},asyncio.get_running_loop().time()+5.0,read_ticket={})
        if type(result) is dict and result.get('state')=='committed' and type(result.get('items')) is list:
            return self._update_batch_result(result,[i.get('memory_id') for i in result['items']],status=True)
        return self._update_batch_result(result,[],status=True)

    async def _guarded_forget_refusal_confirmed(self, body, prepared, original, before_issue):
        """True only if the status phase answers a well-formed 'absent' for this commit's operation on the same transport."""
        try:
            request={**body,'action':'status','selector_kind':None,'selector':None,'preview_token':None,'tombstone_id':None}
            value=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'guarded_batch_forget_status','request':request},
                asyncio.get_running_loop().time()+5.0,read_ticket={})
            return bool(type(value) is dict and set(value)=={'operation_id','state','tombstone_id','closure','recover_until','recovered_at'}
                and value['state']=='absent' and value['operation_id']==body['operation_id']
                and all(value[k] is None for k in ('tombstone_id','closure','recover_until','recovered_at'))
                and self._guarded_preparation is prepared and self._host_bounded_write_available() and before_issue() is True
                and self._guarded_batch_forget_ticket is original and original['transport'] is self._owned_transport
                and original['generation'] is self._owned_generation)
        except Exception:
            return False

    async def _host_guarded_batch_forget(self, request, *, before_issue):
        fields={'version','owner','workspace_id','account_id','policy_projection','action','selector_kind','selector','preview_token','tombstone_id','operation_id'}
        if (type(request) is not dict or set(request)!=fields or type(request.get('version')) is not int or request['version']!=1
                or request.get('action') not in ('preview','commit','status','restore') or not callable(before_issue)):
            raise MemoryRequestRejectedError('guarded forget unavailable')
        body=copy.deepcopy(request); action=body['action']
        self._explicit_add_review_selector({**{k:body[k] for k in ('version','owner','workspace_id','account_id','policy_projection')},'candidate_id':'manualadd_'+'0'*64})
        for key in ('selector_kind','selector','preview_token','tombstone_id','operation_id'):
            if body[key] is not None and (type(body[key]) is not str or not body[key] or len(body[key].encode('utf-8'))>65536):
                raise MemoryRequestRejectedError('guarded forget unavailable')
        if ((action!='restore' and body['tombstone_id'] is not None) or (action=='preview' and (body['selector_kind'] not in ('record_id','source_uri','source_message_id') or body['selector'] is None or body['preview_token'] is not None or body['operation_id'] is not None))
                or (action=='commit' and (body['selector_kind'] not in ('record_id','source_uri','source_message_id') or body['selector'] is None or body['preview_token'] is None or body['operation_id'] is None))
                or (action=='status' and (body['selector_kind'] is not None or body['selector'] is not None or body['preview_token'] is not None or body['operation_id'] is None))
                or (action=='restore' and (body['selector_kind'] is not None or body['selector'] is not None or body['preview_token'] is not None or body['operation_id'] is None or body['tombstone_id'] is None))):
            raise MemoryRequestRejectedError('guarded forget unavailable')
        prepared=self._guarded_preparation
        if prepared is None or prepared.schema_version!=18 or not self._host_bounded_write_available():
            raise MemoryRequestRejectedError('guarded forget unavailable')
        await asyncio.to_thread(prepared.assert_valid)
        if self._guarded_preparation is not prepared or not self._host_bounded_write_available() or before_issue() is not True:
            raise MemoryRequestRejectedError('guarded forget authority changed')
        identity={k:body[k] for k in ('owner','workspace_id','account_id','operation_id')}
        ticket=self._guarded_batch_forget_ticket
        if action in ('commit','restore'):
            if ticket is not None: raise MemoryRequestRejectedError('guarded forget outcome requires status')
            original={'identity':identity,'action':action,'transport':self._owned_transport,'generation':self._owned_generation,
                'process':self._owned_transport.process,'database':(prepared.database_path,prepared.database_id,prepared.device,prepared.inode,prepared.schema_version,prepared._schema_fingerprint)}
            self._guarded_batch_forget_ticket=original; write_status={}
            try:
                value=await self._host_candidate_request(body['owner'],body['workspace_id'],{'phase':'guarded_batch_forget' if action=='commit' else 'guarded_batch_forget_restore','request':body},
                    require_live=True,write_deadline=asyncio.get_running_loop().time()+5.0,before_issue=before_issue,write_status=write_status)
                if action=='commit':
                    if (type(value) is not dict or set(value)!={'tombstone_id','closure','recover_until'} or type(value['tombstone_id']) is not str
                            or not value['tombstone_id'] or type(value['closure']) is not dict or (value['recover_until'] is not None and type(value['recover_until']) is not str)):
                        raise MemoryRequestRejectedError('guarded forget result unavailable')
                elif type(value) is not dict or set(value)!={'raw_ids','candidate_ids','curated_ids','graph_node_ids','native_event_links'} or any(type(v) is not list for v in value.values()):
                    raise MemoryRequestRejectedError('guarded restore result unavailable')
                if (self._guarded_batch_forget_ticket is original and original['transport'] is self._owned_transport
                        and original['generation'] is self._owned_generation and self._host_bounded_write_available()):
                    self._guarded_batch_forget_ticket=None
                return value
            except BaseException as exc:
                pending=write_status.get('ticket')
                known_unsent = pending is None or (pending.get('issued') is False and pending.get('owned_io_drained') is True)
                if self._guarded_batch_forget_ticket is original and known_unsent:
                    self._guarded_batch_forget_ticket=None
                if (action=='commit' and not known_unsent and isinstance(exc, Exception) and pending is not None
                        and pending.get('terminal_error') is True and not pending.get('frame_invalid')
                        and await self._guarded_forget_refusal_confirmed(body,prepared,original,before_issue)):
                    # A clean error frame for this very request (Native serialises writes and answers once) AND the status phase
                    # says this operation is absent: the commit was refused and nothing was applied, so release the pending marker.
                    # A timeout, EOF, invalid frame, or any status other than a well-formed 'absent' keeps it pending (below).
                    self._guarded_batch_forget_ticket=None
                    raise
                if not known_unsent and isinstance(exc, Exception):
                    raise RuntimeError('guarded forget outcome unknown; check original status') from exc
                raise
        if action=='status' and ticket is not None and ticket['identity']!=identity:
            raise MemoryRequestRejectedError('guarded forget original changed')
        phase='guarded_batch_forget_preview' if action=='preview' else 'guarded_batch_forget_status'
        read_ticket={}
        value=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':phase,'request':body},asyncio.get_running_loop().time()+5.0,read_ticket=read_ticket)
        if before_issue() is not True or self._guarded_preparation is not prepared or not self._host_bounded_write_available():
            raise MemoryRequestRejectedError('guarded forget authority changed')
        if action=='preview':
            if type(value) is dict and value=={'state':'unsupported','reason':'f1_edited_head'}:
                raise MemoryRequestRejectedError('This updated memory cannot be forgotten individually yet. Nothing was deleted.', code='f1_edited_head')
            if type(value) is not dict or set(value)!={'selector','closure','token'} or type(value['closure']) is not dict or type(value['token']) is not str or not value['token']:
                raise MemoryRequestRejectedError('guarded forget preview unavailable')
            return value
        terminal = type(value) is dict and value == {'state':'terminal_erased','operation_id':body['operation_id']}
        if not terminal and (type(value) is not dict or set(value)!={'operation_id','state','tombstone_id','closure','recover_until','recovered_at'}
                or value['operation_id']!=body['operation_id'] or value['state'] not in ('absent','committed','restored')):
            raise MemoryRequestRejectedError('guarded forget status unavailable')
        if terminal:
            pass
        elif value['state']=='absent':
            if any(value[k] is not None for k in ('tombstone_id','closure','recover_until','recovered_at')):
                raise MemoryRequestRejectedError('guarded forget status unavailable')
        elif (type(value['tombstone_id']) is not str or not value['tombstone_id'] or type(value['closure']) is not dict
                or (value['recover_until'] is not None and type(value['recover_until']) is not str)
                or (value['recovered_at'] is not None and type(value['recovered_at']) is not str)
                or (value['state']=='committed' and value['recovered_at'] is not None)
                or (value['state']=='restored' and value['recovered_at'] is None)):
            raise MemoryRequestRejectedError('guarded forget status unavailable')
        if ticket is not None:
            if value['state']=='absent' or (ticket['action']=='restore' and value['state'] not in ('restored','terminal_erased')): return {'state':'unknown'}
            if value['state'] in ('committed','restored','terminal_erased') and self._guarded_batch_forget_ticket is ticket:
                transport=self._owned_transport; generation=self._owned_generation
                same=ticket['transport'] is transport and ticket['generation'] is generation
                if not same:
                    old=ticket['transport']; process=ticket['process']
                    try:
                        exited=old.exit_confirmation(ticket['generation'])
                        same=(old.process is process and process.returncode is not None and exited.done()
                            and not exited.cancelled() and exited.result()==process.returncode
                            and read_ticket.get('transport') is transport and read_ticket.get('generation') is generation
                            and ticket['database']==(prepared.database_path,prepared.database_id,prepared.device,prepared.inode,prepared.schema_version,prepared._schema_fingerprint))
                        if same: await asyncio.to_thread(prepared.assert_valid)
                    except Exception: same=False
                if (same and self._guarded_batch_forget_ticket is ticket and self._owned_transport is transport
                        and self._owned_generation is generation and self._guarded_preparation is prepared
                        and self._host_bounded_write_available() and before_issue() is True):
                    self._guarded_batch_forget_ticket=None
        return value

    def _explicit_add_request(self, request, *, status):
        keys={'version','owner','workspace_id','account_id','request_key','policy_projection'}
        keys |= {'input_sha256'} if status else {'content','category','source','question_context','mode'}
        if (type(request) is not dict or set(request)!=keys or type(request.get('version')) is not int or request.get('version')!=1
                or any(type(request.get(k)) is not str or not request[k] or request[k]!=request[k].strip() or len(request[k])>512 for k in ('owner','workspace_id','account_id','request_key'))
                or self._scope(request['owner']).owner!=request['owner'] or request['workspace_id']!=self._workspace_id
                or type(request.get('policy_projection')) is not dict):
            raise MemoryRequestRejectedError('explicit add scope unavailable')
        if status:
            digest=request['input_sha256']
            if type(digest) is not str or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
                raise MemoryRequestRejectedError('explicit add identity unavailable')
        elif (type(request['content']) is not str or type(request['category']) is not str or type(request['source']) is not str
              or request['category'] not in ('fact','contact','project','goal','identity','preference','task','unknown')
              or request['mode'] not in ('manual','review_only') or (request['question_context'] is not None and type(request['question_context']) is not dict)):
            raise MemoryRequestRejectedError('explicit add input unavailable')
        return dict(request)

    @staticmethod
    def _explicit_add_result(result, request, *, status):
        if type(result) is not dict or result.get('state') not in ('absent','applied','staged','terminal_erased'):
            raise MemoryRequestRejectedError('explicit add result unavailable')
        if result['state']=='absent':
            if set(result)!={'state'} or not status: raise MemoryRequestRejectedError('explicit add result unavailable')
            return result
        try:
            encode=lambda value:json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
            identity=['s03.explicit-add-id.v1',request['account_id'],request['workspace_id'],request['request_key']]
            candidate='manualadd_'+hashlib.sha256(encode(identity)).hexdigest()
            digest=request['input_sha256'] if status else hashlib.sha256(encode({
                'category':request['category'],'question_context':request['question_context'],
                'source':request['source'],'text':request['content']})).hexdigest()
        except (KeyError,TypeError,ValueError,UnicodeError) as exc:
            raise MemoryRequestRejectedError('explicit add result unavailable') from exc
        if (set(result)!={'state','candidate_id','input_sha256'} or result.get('candidate_id')!=candidate
                or result.get('input_sha256')!=digest):
            raise MemoryRequestRejectedError('explicit add result unavailable')
        return result

    async def _host_explicit_add_status(self, request):
        body=self._explicit_add_request(request,status=True)
        if not self._explicit_add_profile_ready(): raise MemoryRequestRejectedError('explicit add profile unavailable')
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'explicit_add_status','request':body},asyncio.get_running_loop().time()+self._EXPLICIT_ADD_TIMEOUT,read_ticket={})
        return self._explicit_add_result(result,body,status=True)

    async def _host_explicit_add(self, request, *, before_issue):
        body=self._explicit_add_request(request,status=False)
        if not self._explicit_add_profile_ready() or not callable(before_issue): raise MemoryRequestRejectedError('explicit add profile unavailable')
        result=await self._host_candidate_request(body['owner'],body['workspace_id'],{'phase':'explicit_add','request':body},require_live=True,write_deadline=asyncio.get_running_loop().time()+self._EXPLICIT_ADD_TIMEOUT,before_issue=before_issue)
        result=self._explicit_add_result(result,body,status=False)
        if result['state'] not in ('applied','staged','terminal_erased'): raise MemoryRequestRejectedError('explicit add result unavailable')
        return result

    def _import_review_selector(self, request):
        keys={'version','owner','workspace_id','account_id','batch_id','item_id','suggestion_id','review_key_hash','proposal_fingerprint','mode','policy_projection'}
        if (type(request) is not dict or set(request)!=keys or type(request['version']) is not int or request['version']!=1
            or any(type(request[k]) is not str or not request[k] or request[k]!=request[k].strip() or len(request[k])>512 for k in ('owner','workspace_id','account_id','batch_id','item_id','suggestion_id'))
            or self._scope(request['owner']).owner!=request['owner'] or request['workspace_id']!=self._workspace_id
            or any(type(request[k]) is not str or re.fullmatch(r'[0-9a-f]{64}',request[k]) is None for k in ('review_key_hash','proposal_fingerprint'))
            or request['mode'] not in ('manual','review_only') or type(request['policy_projection']) is not dict):
            raise MemoryRequestRejectedError('import review scope unavailable')
        return dict(request)

    @staticmethod
    def _import_review_result(result, request, *, status):
        material=['s03.import-review-id.v1',request['account_id'],request['workspace_id'],request['batch_id'],request['item_id'],request['suggestion_id'],request['review_key_hash']]
        candidate='importreview_'+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
        if type(result) is not dict or result.get('state') not in ('absent','terminal_erased','applied','staged','not_applied'):
            raise MemoryRequestRejectedError('import review result unavailable')
        state=result['state']
        if state=='absent':
            if not status or set(result)!={'state'}: raise MemoryRequestRejectedError('import review result changed')
        elif state=='terminal_erased':
            if set(result)!={'state','candidate_id'} or result['candidate_id']!=candidate: raise MemoryRequestRejectedError('import review result changed')
        elif state=='not_applied':
            fields={'state','candidate_id','batch_id','item_id','suggestion_id','review_key_hash','proposal_fingerprint','reason'}
            if (set(result)!=fields or result['candidate_id']!=candidate or result['reason'] not in ('preflight_refused','not_issued')
                or any(result[k]!=request[k] for k in ('batch_id','item_id','suggestion_id','review_key_hash','proposal_fingerprint'))):
                raise MemoryRequestRejectedError('import review result changed')
        elif (set(result)!={'state','candidate_id','publication_kind','publication_id'} or result['candidate_id']!=candidate
            or result['publication_kind']!=('memory' if state=='applied' else 'candidate')
            or type(result['publication_id']) is not str or not result['publication_id']
            or (state=='staged' and result['publication_id']!=candidate)):
            raise MemoryRequestRejectedError('import review result changed')
        return result

    @staticmethod
    def _import_review_identity(body):
        return {k:body[k] for k in ('owner','workspace_id','account_id','batch_id','item_id','suggestion_id','review_key_hash','proposal_fingerprint')}

    def _host_import_review_new_allowed(self):
        """Called under the provider's import operation lock before new prepare."""
        return self._import_review_operation_lock.locked() and self._import_review_ticket is None

    def _host_import_reset_available(self):
        """Deny reset admission while import action custody may still exist."""
        return not self._import_review_operation_lock.locked() and self._import_review_ticket is None

    def _host_import_review_local_no_effect(self, *, owner, workspace_id, account_id, batch_id, item_id, suggestion_id, review_key_hash, proposal_fingerprint):
        """Factual exact local outcome; caller owns current human/read authority."""
        identity={'owner':owner,'workspace_id':workspace_id,'account_id':account_id,'batch_id':batch_id,'item_id':item_id,'suggestion_id':suggestion_id,'review_key_hash':review_key_hash,'proposal_fingerprint':proposal_fingerprint}
        if (any(type(identity[k]) is not str or not identity[k] or identity[k]!=identity[k].strip() or len(identity[k])>512 for k in identity)
            or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id
            or any(re.fullmatch(r'[0-9a-f]{64}',identity[k]) is None for k in ('review_key_hash','proposal_fingerprint'))):
            raise MemoryRequestRejectedError('import review scope unavailable')
        ticket=self._import_review_ticket
        if type(ticket) is not dict or ticket.get('import_identity')!=identity or type(ticket.get('import_no_effect')) is not dict:return None
        return self._import_review_result(ticket['import_no_effect'],{'version':1,**identity,'mode':'manual','policy_projection':{}},status=True)

    async def _host_import_review_ack_no_effect(self, store, *, owner, workspace_id, account_id, batch_id, item_id, suggestion_id, review_key_hash, proposal_fingerprint):
        """Retire local no-effect only after the real batch owner proves release."""
        from services.memory.import_batch import MemoryImportBatchStore
        identity={'owner':owner,'workspace_id':workspace_id,'account_id':account_id,'batch_id':batch_id,'item_id':item_id,'suggestion_id':suggestion_id,'review_key_hash':review_key_hash,'proposal_fingerprint':proposal_fingerprint}
        outcome=self._host_import_review_local_no_effect(**identity);ticket=self._import_review_ticket;prepared=self._guarded_preparation
        if (outcome is None or type(ticket) is not dict or type(store) is not MemoryImportBatchStore or prepared is None
            or ticket.get('import_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)
            or os.path.realpath(store.db_path)!=prepared.database_path
            or os.path.realpath(os.fspath(store.data_dir))!=os.path.dirname(prepared.database_path)):
            raise MemoryRequestRejectedError('import review release unavailable')
        try:
            await asyncio.to_thread(prepared.assert_valid)
            original=await asyncio.to_thread(store.read_original_publishing_review,owner=owner,batch_id=batch_id,item_id=item_id,suggestion_id=suggestion_id,review_key_hash=review_key_hash,proposal_fingerprint=proposal_fingerprint,allow_settled=True)
            await asyncio.to_thread(prepared.assert_valid)
        except Exception as exc:
            raise MemoryRequestRejectedError('import review release unavailable') from exc
        review=original.get('review') if type(original) is dict else None
        expected={'version':1,'item_id':item_id,'review_key_hash':review_key_hash,'proposal_fingerprint':proposal_fingerprint}
        if (type(review) is not dict or review.get('state')!='awaiting_review' or review.get('no_effect_original')!=expected
            or review.get('last_failure')!={'code':'IMPORT_REVIEW_NOT_APPLIED','message':'The original review was not applied.','retryable':True}
            or self._import_review_ticket is not ticket or ticket.get('import_identity')!=identity or ticket.get('import_no_effect')!=outcome
            or self._guarded_preparation is not prepared or ticket.get('import_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
            raise MemoryRequestRejectedError('import review release changed')
        self._import_review_ticket=None
        return True

    async def _clear_import_review_ticket(self, ticket, identity, read_ticket):
        if self._import_review_ticket is not ticket or type(ticket) is not dict or ticket.get('import_identity')!=identity:return
        transport=self._owned_transport;generation=self._owned_generation
        if ticket.get('transport') is transport and ticket.get('generation') is generation and transport is not None and transport.current(generation):
            self._import_review_ticket=None;return
        old=ticket.get('transport');process=ticket.get('import_process');prepared=self._guarded_preparation;task=self._owner_task
        if old is None or process is None or old.process is not process or old.generation is not ticket.get('generation') or process.returncode is None:return
        try:
            exited=old.exit_confirmation(ticket['generation'])
            if not exited.done() or exited.cancelled() or exited.result()!=process.returncode:return
        except (RuntimeError,asyncio.CancelledError):return
        if (type(read_ticket) is not dict or prepared is None or read_ticket.get('transport') is not transport or read_ticket.get('generation') is not generation
            or transport is None or not transport.current(generation) or not self._host_bounded_write_available()
            or ticket.get('import_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
            return
        try: await asyncio.to_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error):return
        if (self._import_review_ticket is ticket and ticket.get('import_identity')==identity and old.process is process
            and old.generation is ticket.get('generation') and process.returncode is not None and exited.done() and not exited.cancelled()
            and exited.result()==process.returncode and self._guarded_preparation is prepared and self._owned_transport is transport
            and self._owned_generation is generation and self._owner_task is task and read_ticket.get('transport') is transport
            and read_ticket.get('generation') is generation and self._host_bounded_write_available()
            and ticket.get('import_database')==(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
            self._import_review_ticket=None

    @staticmethod
    def _knowledge_snapshot_review_result(value):
        if type(value) is not dict or value.get('state') not in ('unknown','absent','not_applied','staged','terminal_erased'):
            raise MemoryRequestRejectedError('knowledge snapshot result unavailable')
        if value['state'] in ('staged','terminal_erased'):
            return {'state':value['state'],'candidate_id':value['candidate_id']}
        return {'state':value['state']}

    async def _host_knowledge_snapshot_review_status(self, request):
        body=self._import_review_selector(request)
        if body['mode']!='review_only' or not self._native_memory_ops_ready():
            raise MemoryRequestRejectedError('knowledge snapshot capability unavailable')
        return self._knowledge_snapshot_review_result(await self._host_import_review_status(body))

    async def _host_knowledge_snapshot_review_apply(self, request, *, before_issue):
        body=self._import_review_selector(request)
        if body['mode']!='review_only' or not self._native_memory_ops_ready() or not callable(before_issue):
            raise MemoryRequestRejectedError('knowledge snapshot capability unavailable')
        def guarded_issue():
            return self._native_memory_ops_ready() and before_issue() is True
        return self._knowledge_snapshot_review_result(await self._host_import_review_apply(body, before_issue=guarded_issue))

    async def _host_import_review_status(self, request):
        body=self._import_review_selector(request);identity=self._import_review_identity(body);ticket=self._import_review_ticket
        if type(ticket) is dict and ticket.get('import_identity')==identity:
            no_effect=ticket.get('import_no_effect')
            if type(no_effect) is dict:return self._import_review_result(no_effect,body,status=True)
            if self._owner_rpc_busy or self._bounded_write_reserved or self._owned_poisoned:
                return {'state':'unknown'}
        if not self._explicit_add_profile_ready(): raise MemoryRequestRejectedError('import review profile unavailable')
        read_ticket={}
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'import_review_status','request':body},asyncio.get_running_loop().time()+1.5,read_ticket=read_ticket)
        result=self._import_review_result(result,body,status=True)
        if result['state'] in ('applied','staged','terminal_erased') and type(ticket) is dict:
            await self._clear_import_review_ticket(ticket,identity,read_ticket)
        return result

    async def _host_import_review_apply(self, request, *, before_issue):
        body=self._import_review_selector(request);identity=self._import_review_identity(body);ticket=self._import_review_ticket
        if type(ticket) is dict:
            prior=ticket.get('import_identity');no_effect=ticket.get('import_no_effect')
            if type(no_effect) is dict:
                if prior==identity:return self._import_review_result(no_effect,body,status=True)
                if type(prior) is dict and all(prior.get(k)==identity[k] for k in ('owner','workspace_id','account_id','batch_id','item_id','suggestion_id','review_key_hash')):
                    raise MemoryRequestRejectedError('import review identity conflict')
                self._import_review_ticket=None
            else: raise MemoryRequestRejectedError('import review outcome requires status')
        if not self._explicit_add_profile_ready() or not callable(before_issue): raise MemoryRequestRejectedError('import review profile unavailable')
        prepared=self._guarded_preparation
        if prepared is None: raise MemoryRequestRejectedError('import review owner unavailable')
        original_database=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)
        write_status={}
        try:
            result=await self._host_candidate_request(body['owner'],body['workspace_id'],{'phase':'import_review_apply','request':body},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue,write_status=write_status)
        except MemoryRequestRejectedError:
            ticket=write_status.get('ticket');future=ticket.get('import_future') if type(ticket) is dict else None
            # The owner records its ticket before its only queue insertion.
            # An empty write_status on this completed call therefore proves
            # that this exact invocation never reserved or queued a frame.
            if ticket is None and self._import_review_ticket is None:
                ticket={'import_identity':identity,'issued':False,'import_never_reserved':True,'import_database':original_database}
                self._import_review_ticket=ticket
            if (type(ticket) is dict and self._import_review_ticket is ticket and ticket.get('import_identity')==identity
                and ticket.get('import_never_reserved') is True and ticket.get('issued') is False):
                material=['s03.import-review-id.v1',body['account_id'],body['workspace_id'],body['batch_id'],body['item_id'],body['suggestion_id'],body['review_key_hash']]
                result={'state':'not_applied','candidate_id':'importreview_'+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest(),**{k:body[k] for k in ('batch_id','item_id','suggestion_id','review_key_hash','proposal_fingerprint')},'reason':'not_issued'}
                ticket['import_no_effect']=result
                return self._import_review_result(result,body,status=True)
            if (type(ticket) is dict and self._import_review_ticket is ticket and ticket.get('import_identity')==identity
                and ticket.get('issued') is False and future is not None and future.done() and not self._bounded_write_reserved and not self._owner_rpc_busy
                and ticket.get('transport') is self._owned_transport and ticket.get('generation') is self._owned_generation
                and self._owned_transport is not None and self._owned_transport.current(self._owned_generation)):
                material=['s03.import-review-id.v1',body['account_id'],body['workspace_id'],body['batch_id'],body['item_id'],body['suggestion_id'],body['review_key_hash']]
                result={'state':'not_applied','candidate_id':'importreview_'+hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest(),**{k:body[k] for k in ('batch_id','item_id','suggestion_id','review_key_hash','proposal_fingerprint')},'reason':'not_issued'}
                ticket['import_no_effect']=result
                return self._import_review_result(result,body,status=True)
            raise
        result=self._import_review_result(result,body,status=False);ticket=write_status.get('ticket')
        if (result['state']=='not_applied' and type(ticket) is dict and self._import_review_ticket is ticket
            and ticket.get('issued') is True and ticket.get('import_identity')==identity):
            ticket['import_no_effect']=result
        elif (result['state'] in ('applied','staged','terminal_erased') and type(ticket) is dict
            and self._import_review_ticket is ticket and ticket.get('import_identity')==identity
            and ticket.get('transport') is self._owned_transport and ticket.get('generation') is self._owned_generation
            and self._owned_transport is not None and self._owned_transport.current(self._owned_generation)):
            self._import_review_ticket=None
        return result

    def _explicit_add_review_selector(self, selector, *, import_origin=False):
        keys={'version','owner','workspace_id','account_id','candidate_id','policy_projection'}
        if (type(selector) is not dict or set(selector)!=keys or type(selector.get('version')) is not int or selector['version']!=1
                or any(type(selector.get(k)) is not str or not selector[k] or selector[k]!=selector[k].strip() or len(selector[k])>512 for k in ('owner','workspace_id','account_id','candidate_id'))
                or re.fullmatch((r'importreview_' if import_origin else r'manualadd_')+r'[0-9a-f]{64}',selector['candidate_id']) is None
                or type(selector['policy_projection']) is not dict or self._scope(selector['owner']).owner!=selector['owner'] or selector['workspace_id']!=self._workspace_id):
            raise MemoryRequestRejectedError('explicit Add review scope unavailable')
        return dict(selector)

    def _explicit_add_review_request(self, request, *, edit, import_origin=False):
        keys={'selector','expected_revision','expected_binding'} | ({'text','category','source','question_context'} if edit else {'review_key','action'})
        if type(request) is not dict or set(request)!=keys: raise MemoryRequestRejectedError('explicit Add review unavailable')
        selector=self._explicit_add_review_selector(request['selector'],import_origin=import_origin)
        binding=request['expected_binding']; revision=request['expected_revision']
        if (type(revision) is not int or revision<1 or type(binding) is not str or len(binding)!=64 or any(c not in '0123456789abcdef' for c in binding)):
            raise MemoryRequestRejectedError('explicit Add review binding unavailable')
        if edit:
            if (type(request['text']) is not str or type(request['source']) is not str or request['category'] not in ('fact','contact','project','goal','identity','preference','task','unknown')
                    or (request['question_context'] is not None and type(request['question_context']) is not dict)):
                raise MemoryRequestRejectedError('explicit Add edit unavailable')
        elif (type(request['review_key']) is not str or not request['review_key'] or request['review_key']!=request['review_key'].strip()
              or len(request['review_key'])>512 or request['action'] not in ('accept','reject')):
            raise MemoryRequestRejectedError('explicit Add review identity unavailable')
        return {**request,'selector':selector}

    @staticmethod
    def _explicit_add_review_preview_result(result, selector):
        if (type(result) is not dict or set(result)!={'state','candidate_id','proposal','expected_revision','binding_sha256'}
                or result['state']!='preview' or result['candidate_id']!=selector['candidate_id'] or type(result['proposal']) is not dict
                or type(result['expected_revision']) is not int or result['expected_revision']<1 or type(result['binding_sha256']) is not str
                or len(result['binding_sha256'])!=64 or any(c not in '0123456789abcdef' for c in result['binding_sha256'])):
            raise MemoryRequestRejectedError('explicit Add review result unavailable')
        proposal=result['proposal']
        if (proposal.get('candidate_id')!=selector['candidate_id'] or proposal.get('owner')!=selector['owner']
                or proposal.get('workspace_id')!=selector['workspace_id'] or proposal.get('account_id')!=selector['account_id']):
            raise MemoryRequestRejectedError('explicit Add review result changed')
        return result

    @staticmethod
    def _explicit_add_review_original_result(result, request, *, status):
        if type(result) is not dict or result.get('state') not in ('absent','applied','rejected','terminal_erased','not_applied'):
            raise MemoryRequestRejectedError('explicit Add review result unavailable')
        if result['state']=='absent':
            if not status or set(result)!={'state'}: raise MemoryRequestRejectedError('explicit Add review result unavailable')
        elif result['state']=='not_applied':
            if (status or set(result)!={'state','candidate_id','expected_revision','expected_binding','review_key','action','reason'}
                    or result['candidate_id']!=request['selector']['candidate_id'] or any(result[k]!=request[k] for k in ('expected_revision','expected_binding','review_key','action'))
                    or result['reason'] not in ('conflict','policy_changed')):raise MemoryRequestRejectedError('explicit Add review result changed')
        elif result['state']=='terminal_erased':
            if set(result)!={'state','candidate_id'} or result['candidate_id']!=request['selector']['candidate_id']:raise MemoryRequestRejectedError('explicit Add review result changed')
        elif (set(result)!={'state','candidate_id','curated_id'} or result['candidate_id']!=request['selector']['candidate_id']
              or (result['state']=='applied' and (request['action']!='accept' or type(result['curated_id']) is not str or not result['curated_id']))
              or (result['state']=='rejected' and (request['action']!='reject' or result['curated_id'] is not None))):
            raise MemoryRequestRejectedError('explicit Add review result changed')
        return result

    async def _host_guarded_full_memory_preview(self, *, owner, workspace_id, account_id, components, deadline):
        """Read the existing guarded reset owner's account-bound full preview."""
        if (any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace_id,account_id))
                or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id
                or type(components) is not list or not 1<=len(components)<=3
                or any(type(v) is not str or v not in ('memories','graph','ingest') for v in components) or len(set(components))!=len(components)
                or 'memories' not in components or type(deadline) is not float or not math.isfinite(deadline)
                or asyncio.get_running_loop().time()>=deadline):
            raise MemoryRequestRejectedError('full memory preview unavailable')
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        if prepared is None or not self._host_bounded_write_available(): raise MemoryRequestRejectedError('full memory owner unavailable')
        try: await asyncio.to_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('full memory owner unavailable') from exc
        if (self._guarded_preparation is not prepared or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('full memory owner changed')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'guarded_full_memory_preview','version':1,'account_id':account_id,'components':list(components)},deadline,read_ticket={})
        try: await asyncio.to_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('full memory owner unavailable') from exc
        if (self._guarded_preparation is not prepared or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or not self._host_bounded_write_available() or asyncio.get_running_loop().time()>=deadline
                or type(result) is not dict or set(result)!={'components','expanded_components','implications'}
                or result.get('expanded_components')!=['memories','graph','ingest'] or type(result.get('implications')) is not list
                or any(type(v) is not str for v in result['implications']) or type(result.get('components')) is not dict
                or any(type(result['components'].get(key)) is not dict or type(result['components'][key].get('count')) is not int
                       or result['components'][key]['count']<0 or type(result['components'][key].get('fingerprint')) is not str
                       or re.fullmatch(r'[0-9a-f]{64}',result['components'][key]['fingerprint']) is None
                       for key in ('memories','graph','ingest'))):
            raise MemoryRequestRejectedError('full memory preview unavailable')
        return result

    async def _host_selective_ingest_preview(self, *, owner, workspace_id, account_id, deadline):
        """Read a private account-bound preview on the existing guarded owner."""
        if (any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace_id,account_id))
                or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id
                or type(deadline) is not float or not math.isfinite(deadline) or asyncio.get_running_loop().time()>=deadline):
            raise MemoryRequestRejectedError('selective ingest preview unavailable')
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        if prepared is None or not self._host_bounded_write_available(): raise MemoryRequestRejectedError('selective ingest owner unavailable')
        try: await asyncio.to_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('selective ingest owner unavailable') from exc
        if (self._guarded_preparation is not prepared or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('selective ingest owner changed')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'guarded_selective_ingest_preview','version':1,'account_id':account_id,'components':['ingest']},deadline,read_ticket={})
        try: await asyncio.to_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('selective ingest owner unavailable') from exc
        if (self._guarded_preparation is not prepared or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or not self._host_bounded_write_available() or asyncio.get_running_loop().time()>=deadline
                or type(result) is not dict or set(result)!={'components','expanded_components','implications'}
                or type(result['components']) is not dict or type(result['components'].get('ingest')) is not dict
                or result.get('expanded_components')!=['ingest'] or type(result.get('implications')) is not list):
            raise MemoryRequestRejectedError('selective ingest preview unavailable')
        selective=result['components']['ingest'].get('selective')
        if (type(selective) is not dict or selective.get('version')!=1 or selective.get('state')!='complete'
                or type(selective.get('partition_sha256')) is not str or len(selective['partition_sha256'])!=64
                or any(c not in '0123456789abcdef' for c in selective['partition_sha256'])):
            raise MemoryRequestRejectedError('selective ingest preview unavailable')
        return result

    async def _host_explicit_add_review_preview(self, selector):
        body=self._explicit_add_review_selector(selector)
        if not self._explicit_add_profile_ready(): raise MemoryRequestRejectedError('explicit Add profile unavailable')
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'explicit_add_review_preview','request':body},asyncio.get_running_loop().time()+1.5,read_ticket={})
        return self._explicit_add_review_preview_result(result,body)

    async def _host_import_staged_review_preview(self, selector):
        body=self._explicit_add_review_selector(selector,import_origin=True)
        if not self._explicit_add_profile_ready(): raise MemoryRequestRejectedError('import review profile unavailable')
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'import_staged_review_preview','request':body},asyncio.get_running_loop().time()+1.5,read_ticket={})
        return self._explicit_add_review_preview_result(result,body)

    async def _host_import_staged_review_edit(self, request, *, before_issue):
        body=self._explicit_add_review_request(request,edit=True,import_origin=True);selector=body['selector']
        if body['source']!='memory_import' or not self._explicit_add_profile_ready() or not callable(before_issue): raise MemoryRequestRejectedError('import review profile unavailable')
        result=await self._host_candidate_request(selector['owner'],selector['workspace_id'],{'phase':'import_staged_review_edit','request':body},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue)
        result=self._explicit_add_review_preview_result(result,selector)
        if (result['expected_revision']!=body['expected_revision']+3 or result['proposal']['category']!=body['category']
                or result['proposal']['source']!='memory_import'):
            raise MemoryRequestRejectedError('import review edit changed')
        return result

    async def _host_explicit_add_review_edit(self, request, *, before_issue):
        body=self._explicit_add_review_request(request,edit=True);selector=body['selector']
        if not self._explicit_add_profile_ready() or not callable(before_issue): raise MemoryRequestRejectedError('explicit Add profile unavailable')
        result=await self._host_candidate_request(selector['owner'],selector['workspace_id'],{'phase':'explicit_add_review_edit','request':body},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue)
        result=self._explicit_add_review_preview_result(result,selector)
        if (result['expected_revision']!=body['expected_revision']+3
                or result['proposal']['category']!=body['category']):
            raise MemoryRequestRejectedError('explicit Add edit result changed')
        return result

    async def _host_explicit_add_review_status(self, request, *, import_origin=False):
        body=self._explicit_add_review_request(request,edit=False,import_origin=import_origin);selector=body['selector']
        ticket=self._explicit_add_review_ticket;read_ticket={}
        local=self._explicit_add_review_noop(ticket,body)
        if local is not None:return local
        if not self._explicit_add_profile_ready(): raise MemoryRequestRejectedError('explicit Add profile unavailable')
        result=await self._s07_owner_rpc(selector['owner'],selector['workspace_id'],{'phase':'import_staged_review_status' if import_origin else 'explicit_add_review_status','request':body},asyncio.get_running_loop().time()+1.5,read_ticket=read_ticket)
        result=self._explicit_add_review_original_result(result,body,status=True)
        if result['state'] in ('applied','rejected','terminal_erased'):
            await self._clear_explicit_add_review_ticket(body,ticket,read_ticket)
        return result

    def _explicit_add_review_noop(self, ticket, request):
        if type(ticket) is not dict or self._explicit_add_review_ticket is not ticket or ticket.get('io_settled') is not True:return None
        identity=ticket.get('explicit_add_identity');result=ticket.get('not_applied')
        selector=request['selector'];expected={**{k:selector[k] for k in ('owner','workspace_id','account_id','candidate_id')},
            **{k:request[k] for k in ('expected_revision','expected_binding','review_key','action')}}
        if identity!=expected or type(result) is not dict:return None
        try:return self._explicit_add_review_original_result(result,request,status=False)
        except MemoryRequestRejectedError:return None

    def _retire_explicit_add_noop(self, ticket):
        if type(ticket) is not dict or self._explicit_add_review_ticket is not ticket or ticket.get('io_settled') is not True or self._bounded_write_reserved:return False
        identity=ticket.get('explicit_add_identity')
        if type(identity) is not dict:return False
        selector={k:identity.get(k) for k in ('owner','workspace_id','account_id','candidate_id')}
        request={'selector':selector,**{k:identity.get(k) for k in ('expected_revision','expected_binding','review_key','action')}}
        if self._explicit_add_review_noop(ticket,request) is None:return False
        self._explicit_add_review_ticket=None;return True

    async def _clear_explicit_add_review_ticket(self, request, ticket, read_ticket=None):
        identity=ticket.get('explicit_add_identity') if type(ticket) is dict else None
        selector=request['selector']
        expected={**{k:selector[k] for k in ('owner','workspace_id','account_id','candidate_id')},
                  **{k:request[k] for k in ('expected_revision','expected_binding','review_key','action')}}
        if ticket is None or self._explicit_add_review_ticket is not ticket or identity!=expected:return
        transport=self._owned_transport;generation=self._owned_generation
        if (ticket.get('transport') is transport and ticket.get('generation') is generation
                and transport is not None and transport.current(generation)):
            self._explicit_add_review_ticket=None;return
        # A fresh guarded child may reconcile an old exact ticket only after
        # positive exit of its original process and a read on this same DB.
        old=ticket.get('transport');process=ticket.get('explicit_add_process');prepared=self._guarded_preparation;task=self._owner_task
        if old is None or process is None or old.process is not process or old.generation is not ticket.get('generation') or process.returncode is None:return
        try:
            exited=old.exit_confirmation(ticket['generation'])
            if not exited.done() or exited.cancelled() or exited.result()!=process.returncode:return
        except (RuntimeError,asyncio.CancelledError):return
        if (type(read_ticket) is not dict
                or prepared is None or read_ticket.get('transport') is not transport or read_ticket.get('generation') is not generation
                or transport is None or not transport.current(generation) or not self._host_bounded_write_available()
                or ticket.get('explicit_add_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
            return
        try: await asyncio.to_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error):return
        if (self._explicit_add_review_ticket is ticket and ticket.get('explicit_add_identity')==expected
                and old.process is process and old.generation is ticket.get('generation') and process.returncode is not None
                and exited.done() and not exited.cancelled() and exited.result()==process.returncode and self._guarded_preparation is prepared
                and self._owned_transport is transport and self._owned_generation is generation and self._owner_task is task
                and read_ticket.get('transport') is transport and read_ticket.get('generation') is generation
                and self._host_bounded_write_available() and ticket.get('explicit_add_database')==(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
            self._explicit_add_review_ticket=None

    async def _host_explicit_add_missing_page(self, request, *, terminal, import_origin=False):
        fields={'version','owner','workspace_id','account_id','policy_projection','after','limit'}
        if (type(request) is not dict or set(request)!=fields or type(request['version']) is not int or request['version']!=1
            or any(type(request[k]) is not str or not request[k] or request[k]!=request[k].strip() or len(request[k])>512 for k in ('owner','workspace_id','account_id'))
            or type(request['policy_projection']) is not dict or type(request['limit']) is not int or not 1<=request['limit']<=20
            or (request['after'] is not None and type(request['after']) is not str)):
            raise MemoryRequestRejectedError('explicit Add page unavailable')
        if not self._explicit_add_profile_ready(): raise MemoryRequestRejectedError('explicit Add profile unavailable')
        phase=('import_missing_terminal_page' if terminal else 'import_missing_original_page') if import_origin else ('explicit_add_missing_terminal_page' if terminal else 'explicit_add_missing_original_page')
        result=await self._s07_owner_rpc(request['owner'],request['workspace_id'],{'phase':phase,'request':request},asyncio.get_running_loop().time()+1.5,read_ticket={})
        prefix=('outbox_s04_import_review_erased_' if terminal else 'outbox_cs_import_staged_review_') if import_origin else ('outbox_s04_explicit_add_erased_' if terminal else 'outbox_cs_explicit_add_review_')
        suffix='' if terminal else ('_import_staged_review' if import_origin else '_explicit_add_review')
        cursor=result.get('next_cursor') if type(result) is dict else None
        if (type(result) is not dict or set(result)!={'version','items','next_cursor','partial'} or type(result['version']) is not int or result['version']!=1
            or type(result['items']) is not list or len(result['items'])>request['limit'] or type(result['partial']) is not bool
            or (cursor is not None and (type(cursor) is not str or re.fullmatch(re.escape(prefix)+r'[0-9a-f]{64}'+re.escape(suffix),cursor) is None))
            or result['partial']!=(cursor is not None)):
            raise MemoryRequestRejectedError('explicit Add page changed')
        seen=set()
        for item in result['items']:
            if type(item) is not dict or type(item.get('candidate_id')) is not str or not re.fullmatch(r'importreview_[0-9a-f]{64}' if import_origin else r'manualadd_[0-9a-f]{64}',item['candidate_id']) or item['candidate_id'] in seen:
                raise MemoryRequestRejectedError('explicit Add page changed')
            seen.add(item['candidate_id'])
            if terminal:
                if set(item)!={'candidate_id','state'} or item['state']!='terminal_erased': raise MemoryRequestRejectedError('explicit Add page changed')
            else:
                if set(item)!={'candidate_id','original','receipt'} or type(item['original']) is not dict: raise MemoryRequestRejectedError('explicit Add page changed')
                original=item['original']; selector={'version':1,'owner':request['owner'],'workspace_id':request['workspace_id'],'account_id':request['account_id'],'policy_projection':request['policy_projection'],'candidate_id':item['candidate_id']}
                if set(original)!={'candidate_id','expected_revision','expected_binding','review_key','action'} or original['candidate_id']!=item['candidate_id']:
                    raise MemoryRequestRejectedError('explicit Add page changed')
                body={'selector':selector,**{k:original[k] for k in ('expected_revision','expected_binding','review_key','action')}}
                self._explicit_add_review_request(body,edit=False,import_origin=import_origin)
                self._explicit_add_review_original_result(item['receipt'],body,status=True)
                if item['receipt']['state'] not in ('applied','rejected'): raise MemoryRequestRejectedError('explicit Add page changed')
        return result

    async def _host_explicit_add_missing_original_page(self, request):
        return await self._host_explicit_add_missing_page(request,terminal=False)

    async def _host_explicit_add_missing_terminal_page(self, request):
        return await self._host_explicit_add_missing_page(request,terminal=True)

    async def _host_import_missing_original_page(self, request):
        return await self._host_explicit_add_missing_page(request,terminal=False,import_origin=True)

    async def _host_import_missing_terminal_page(self, request):
        return await self._host_explicit_add_missing_page(request,terminal=True,import_origin=True)

    async def _host_explicit_add_review_original(self, selector, *, import_origin=False):
        body=self._explicit_add_review_selector(selector,import_origin=import_origin)
        ticket=self._explicit_add_review_ticket;identity=ticket.get('explicit_add_identity') if type(ticket) is dict else None
        local=None
        if type(identity) is dict and all(identity.get(k)==body[k] for k in ('owner','workspace_id','account_id','candidate_id')):
            original={k:identity[k] for k in ('candidate_id','expected_revision','expected_binding','review_key','action')}
            local_request={'selector':body,**{k:original[k] for k in ('expected_revision','expected_binding','review_key','action')}}
            no_effect=self._explicit_add_review_noop(ticket,local_request)
            local={'state':'found','original':original,'receipt':no_effect if no_effect is not None else {'state':'unknown','candidate_id':body['candidate_id'],'curated_id':None}}
            if no_effect is not None:return local
        if local is not None and (self._owner_rpc_busy or self._bounded_write_reserved or self._owned_poisoned):return local
        if not self._explicit_add_profile_ready():
            if local is not None:return local
            raise MemoryRequestRejectedError('explicit Add profile unavailable')
        read_ticket={}
        result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'import_staged_review_original' if import_origin else 'explicit_add_review_original','request':body},asyncio.get_running_loop().time()+1.5,read_ticket=read_ticket)
        if type(result) is dict and result.get('state')=='terminal_erased':
            if set(result)!={'state','candidate_id'} or result['candidate_id']!=body['candidate_id']:raise MemoryRequestRejectedError('explicit Add original changed')
            if local is not None:
                exact=self._explicit_add_review_request({'selector':body,**{k:local['original'][k] for k in ('expected_revision','expected_binding','review_key','action')}},edit=False,import_origin=import_origin)
                settled=await self._host_explicit_add_review_status(exact,import_origin=import_origin)
                if settled.get('state')!='terminal_erased' or self._explicit_add_review_ticket is ticket:return local
            return result
        if result is None or result=={'state':'absent'}:
            if local is not None:return local
            if result=={'state':'absent'}:return result
            raise MemoryRequestRejectedError('explicit Add original unavailable')
        if (type(result) is not dict or set(result)!={'state','original','receipt'} or result['state']!='found' or type(result['original']) is not dict
                or set(result['original'])!={'candidate_id','expected_revision','expected_binding','review_key','action'}):
            raise MemoryRequestRejectedError('explicit Add original unavailable')
        original=result['original']
        request=self._explicit_add_review_request({'selector':body,**{k:original[k] for k in ('expected_revision','expected_binding','review_key','action')}},edit=False,import_origin=import_origin)
        if original['candidate_id']!=body['candidate_id']:raise MemoryRequestRejectedError('explicit Add original changed')
        factual=self._explicit_add_review_original_result(result['receipt'],request,status=True)
        if factual['state'] not in ('applied','rejected'):raise MemoryRequestRejectedError('explicit Add original unavailable')
        if local is not None:
            if local['original']!=original:
                # The immutable candidate-bound receipt proves a different
                # request cannot apply; still require this exact I/O to settle.
                old=ticket.get('transport');process=ticket.get('explicit_add_process')
                settled=(ticket.get('io_settled') is True and old is self._owned_transport
                         and ticket.get('generation') is self._owned_generation and self._host_bounded_write_available())
                exited=bool(process is not None and process.returncode is not None)
                if settled or exited:
                    local_request=self._explicit_add_review_request({'selector':body,**{k:local['original'][k] for k in ('expected_revision','expected_binding','review_key','action')}},edit=False,import_origin=import_origin)
                    await self._clear_explicit_add_review_ticket(local_request,ticket,read_ticket)
                raise MemoryRequestRejectedError('explicit Add original review conflict')
            await self._clear_explicit_add_review_ticket(request,ticket,read_ticket)
        return result

    async def _host_explicit_add_review_apply(self, request, *, before_issue, import_origin=False):
        body=self._explicit_add_review_request(request,edit=False,import_origin=import_origin);selector=body['selector']
        if not self._explicit_add_profile_ready() or not callable(before_issue): raise MemoryRequestRejectedError('explicit Add profile unavailable')
        prior=self._explicit_add_review_ticket
        prior_identity=prior.get('explicit_add_identity') if type(prior) is dict else None
        if type(prior_identity) is dict and any(prior_identity.get(k)!=selector[k] for k in ('owner','workspace_id','account_id','candidate_id')):
            # A positively completed no-effect intent in another candidate
            # must not fence a new deliberate review.
            self._retire_explicit_add_noop(prior)
        original=await self._host_explicit_add_review_original(selector,import_origin=import_origin)
        if original.get('state')=='terminal_erased':return original
        if original.get('state')=='found':
            expected={'candidate_id':selector['candidate_id'],**{k:body[k] for k in ('expected_revision','expected_binding','review_key','action')}}
            factual=original.get('receipt',{})
            if factual.get('state')=='not_applied':
                if original.get('original')==expected:return self._explicit_add_review_original_result(factual,body,status=False)
                if original.get('original',{}).get('review_key')==body['review_key']:raise MemoryRequestRejectedError('explicit Add review identity conflict')
                if not self._retire_explicit_add_noop(self._explicit_add_review_ticket):raise MemoryRequestRejectedError('explicit Add review outcome requires status')
                original=await self._host_explicit_add_review_original(selector,import_origin=import_origin)
            else:
                if original.get('original')!=expected:raise MemoryRequestRejectedError('explicit Add original review conflict')
                if factual.get('state')=='unknown':raise MemoryRequestRejectedError('explicit Add review outcome requires status')
                return self._explicit_add_review_original_result(factual,body,status=True)
        if original!={'state':'absent'}:raise MemoryRequestRejectedError('explicit Add original unavailable')
        write_status={}
        result=await self._host_candidate_request(selector['owner'],selector['workspace_id'],{'phase':'import_staged_review_apply' if import_origin else 'explicit_add_review_apply','request':body},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue,write_status=write_status)
        result=self._explicit_add_review_original_result(result,body,status=False)
        ticket=write_status.get('ticket')
        if result['state']=='not_applied':
            if type(ticket) is not dict or self._explicit_add_review_ticket is not ticket or ticket.get('io_settled') is not True:
                raise MemoryRequestRejectedError('explicit Add review outcome unavailable')
            ticket['not_applied']=dict(result)
        else:
            await self._clear_explicit_add_review_ticket(body,ticket)
        return result

    async def _host_import_staged_review_status(self, request):
        return await self._host_explicit_add_review_status(request,import_origin=True)

    async def _host_import_staged_review_original(self, selector):
        return await self._host_explicit_add_review_original(selector,import_origin=True)

    async def _host_import_staged_review_apply(self, request, *, before_issue):
        return await self._host_explicit_add_review_apply(request,before_issue=before_issue,import_origin=True)

    async def _host_phase_root_observation(self,request,*,renew,before_issue):
        if type(request) is not dict or not callable(before_issue):raise MemoryRequestRejectedError('phase observation authority unavailable')
        try:
            encoded=json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(encoded.encode('utf-8'))>65536:raise ValueError('request too large')
            request=json.loads(encoded)
        except (TypeError,ValueError,OverflowError):raise MemoryRequestRejectedError('phase observation request unavailable') from None
        proof=request.get('transition' if renew else 'phase');owner=proof.get('new_owner' if renew else 'owner') if type(proof) is dict else None;workspace=request.get('workspace_id')
        if any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace)):
            raise MemoryRequestRejectedError('phase observation scope unavailable')
        phase='phase_root_observation_renew' if renew else 'phase_root_observation_fence'
        result=await self._host_candidate_request(owner,workspace,{'phase':phase,'request':request},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue)
        if not renew:
            if (type(result) is not dict or set(result)!={'version','state','fence_id'} or type(result['version']) is not int or result['version']!=1
                or result['state']!='fenced' or type(result['fence_id']) is not str or re.fullmatch(r'cs_s04pair_[0-9a-f]{64}',result['fence_id']) is None):
                raise MemoryRequestRejectedError('phase observation receipt unavailable')
        else:
            base={'version','state','source_id','revision'}
            if type(result) is not dict or type(result.get('version')) is not int or result['version']!=3 or result.get('state') not in ('renewed','already_applied','restored'):
                raise MemoryRequestRejectedError('phase observation receipt unavailable')
            expected=base|({'root_renewal_id','rebind_id','rebind_sha256'} if result['state']=='restored' else {'receipt_id','receipt_sha256'})
            source=request.get('source')
            if type(source) is not dict or not all(key in source for key in ('version','account_id','chat_id','actor_id','message_id','part_id','revision')):
                raise MemoryRequestRejectedError('phase observation receipt unavailable')
            material=json.dumps([source[key] for key in ('version','account_id','chat_id','actor_id','message_id','part_id')],ensure_ascii=False,separators=(',',':')).encode('utf-8')
            expected_source='hostpart_'+hashlib.sha256(material).hexdigest()
            if (set(result)!=expected or result['source_id']!=expected_source
                or type(result['revision']) is not int or result['revision']!=source.get('revision')):
                raise MemoryRequestRejectedError('phase observation receipt unavailable')
            if result['state']=='restored':
                if type(result['root_renewal_id']) is not str or re.fullmatch(r'cs_s04pairrenew_[0-9a-f]{64}',result['root_renewal_id']) is None or (result['rebind_id'] is None)!=(result['rebind_sha256'] is None):
                    raise MemoryRequestRejectedError('phase observation receipt unavailable')
                if result['rebind_id'] is not None and (type(result['rebind_id']) is not str or re.fullmatch(r'cs_s06renew_[0-9a-f]{64}',result['rebind_id']) is None or type(result['rebind_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['rebind_sha256']) is None):
                    raise MemoryRequestRejectedError('phase observation receipt unavailable')
            elif (type(result['receipt_id']) is not str or re.fullmatch(r'cs_s06renew_[0-9a-f]{64}',result['receipt_id']) is None or type(result['receipt_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['receipt_sha256']) is None):
                raise MemoryRequestRejectedError('phase observation receipt unavailable')
        return result

    async def _host_phase_root_observation_fence(self,request,*,before_issue):
        return await self._host_phase_root_observation(request,renew=False,before_issue=before_issue)

    async def _host_phase_root_observation_renew(self,request,*,before_issue):
        return await self._host_phase_root_observation(request,renew=True,before_issue=before_issue)

    async def _host_phase_root_renew(self,request,*,before_issue):
        if type(request) is not dict or not callable(before_issue):raise MemoryRequestRejectedError('phase root authority unavailable')
        try:
            encoded=json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(encoded.encode('utf-8'))>65536:raise ValueError('request too large')
            request=json.loads(encoded)
        except (TypeError,ValueError,OverflowError):raise MemoryRequestRejectedError('phase root request unavailable') from None
        transition=request.get('transition');owner=transition.get('new_owner') if type(transition) is dict else None;workspace=request.get('workspace_id')
        if any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace)):
            raise MemoryRequestRejectedError('phase root scope unavailable')
        result=await self._host_candidate_request(owner,workspace,{'phase':'phase_root_renew','request':request},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue)
        if (type(result) is not dict or set(result)!={'version','state','receipt_id'} or type(result['version']) is not int or result['version']!=1
            or result['state'] not in ('renewed','absent') or type(result['receipt_id']) is not str
            or re.fullmatch(r'cs_s04pair(?:renew)?_[0-9a-f]{64}',result['receipt_id']) is None):
            raise MemoryRequestRejectedError('phase root receipt unavailable')
        return result

    async def _host_phase_root_fence(self,request,*,before_issue):
        if type(request) is not dict or not callable(before_issue):raise MemoryRequestRejectedError('phase root authority unavailable')
        try:
            encoded=json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(encoded.encode('utf-8'))>65536:raise ValueError('request too large')
            request=json.loads(encoded)
        except (TypeError,ValueError,OverflowError):raise MemoryRequestRejectedError('phase root request unavailable') from None
        phase=request.get('phase');owner=phase.get('owner') if type(phase) is dict else None;workspace=request.get('workspace_id')
        if any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace)):
            raise MemoryRequestRejectedError('phase root scope unavailable')
        result=await self._host_candidate_request(owner,workspace,{'phase':'phase_root_fence','request':request},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue)
        if (type(result) is not dict or set(result)!={'version','state','fence_id'} or type(result['version']) is not int or result['version']!=1
            or result['state'] not in ('fenced','absent') or type(result['fence_id']) is not str
            or re.fullmatch(r'cs_s04pair_[0-9a-f]{64}',result['fence_id']) is None):
            raise MemoryRequestRejectedError('phase root receipt unavailable')
        return result

    async def _host_source_publication_original(self, request, *, current_guard):
        """Read only this original publication; unknown is never permission to resend."""
        from src.openclank.memory_job_admission import _completed_turn_thread
        if type(request) is not dict or not callable(current_guard) or current_guard() is not True:
            raise MemoryRequestRejectedError('source publication original unavailable')
        try:
            encoded=json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(encoded.encode('utf-8'))>65536: raise ValueError('request too large')
            request=json.loads(encoded)
        except (TypeError,ValueError,OverflowError) as exc:
            raise MemoryRequestRejectedError('source publication original unavailable') from exc
        owner=request.get('owner');workspace=request.get('workspace_id');source=request.get('source')
        if _provider_source_mapping_error(self, source): raise MemoryRequestRejectedError('source publication original unavailable')
        ticket=self._paired_review_ticket
        if (type(owner) is not str or not owner or type(workspace) is not str or not workspace or type(source) is not dict
            or type(ticket) is not dict or type(ticket.get('source_publication_identity')) is not dict):
            raise MemoryRequestRejectedError('source publication original unavailable')
        prior=copy.deepcopy(ticket['source_publication_identity'])
        if {k:v for k,v in prior.items() if k!='owner'}!={k:v for k,v in request.items() if k!='owner'}:
            raise MemoryRequestRejectedError('source publication original changed')
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        process=transport.process if transport is not None else None
        if prepared is None or process is None or ticket.get('source_publication_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode):
            raise MemoryRequestRejectedError('source publication custody unavailable')
        _,cancelled=await _completed_turn_thread(prepared.assert_valid)
        if cancelled: raise asyncio.CancelledError
        if current_guard() is not True: raise MemoryRequestRejectedError('source publication authority changed')
        read_ticket={}
        result=await self._s07_owner_rpc(owner,workspace,{'phase':'source_publication_original','request':request},
            asyncio.get_running_loop().time()+1.5,read_ticket=read_ticket)
        if _provider_source_mapping_error(self, source): raise MemoryRequestRejectedError('source publication profile changed')
        if result=={'version':1,'state':'unknown'} and type(result.get('version')) is int:
            if (current_guard() is not True or self._paired_review_ticket is not ticket or ticket.get('source_publication_identity')!=prior
                or self._guarded_preparation is not prepared or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or read_ticket.get('transport') is not transport or read_ticket.get('generation') is not generation
                or transport is None or not transport.current(generation) or transport.process is not process):
                raise MemoryRequestRejectedError('source publication custody changed')
            return result
        expected='s05candidate_'+hashlib.sha256(json.dumps([source.get('account_id'),request.get('job_id')],ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        if result!={'version':1,'state':'applied','candidate_id':expected} or type(result.get('version')) is not int:
            raise MemoryRequestRejectedError('source publication receipt unavailable')
        old=ticket.get('transport');old_process=ticket.get('source_publication_process');settled=ticket.get('pending_pair_io_settled') is True
        if not settled and old is not None and old_process is not None and old.process is old_process and old.generation is ticket.get('generation') and old_process.returncode is not None:
            try:
                exited=old.exit_confirmation(ticket['generation'])
                settled=exited.done() and not exited.cancelled() and exited.result()==old_process.returncode
            except (RuntimeError,asyncio.CancelledError): settled=False
        _,cancelled=await _completed_turn_thread(prepared.assert_valid)
        if cancelled: raise asyncio.CancelledError
        if (not settled or current_guard() is not True or self._paired_review_ticket is not ticket or ticket.get('source_publication_identity')!=prior
            or self._guarded_preparation is not prepared or self._owned_transport is not transport or self._owned_generation is not generation or self._owner_task is not task
            or read_ticket.get('transport') is not transport or read_ticket.get('generation') is not generation or transport is None or not transport.current(generation)
            or transport.process is not process or ticket.get('source_publication_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
            raise MemoryRequestRejectedError('source publication custody changed')
        ticket['source_publication_fact']=copy.deepcopy(result)
        return result

    async def _ack_source_publication(self, request, *, current_guard):
        """Release the one ticket only after factual publication and exact settlement."""
        from src.openclank.memory_job_admission import _completed_turn_thread, _source_publication_settled
        if not callable(current_guard) or current_guard() is not True: raise MemoryRequestRejectedError('source publication acknowledgment unavailable')
        ticket=self._paired_review_ticket
        if type(ticket) is not dict or type(ticket.get('source_publication_identity')) is not dict:
            raise MemoryRequestRejectedError('source publication acknowledgment unavailable')
        identity=copy.deepcopy(ticket['source_publication_identity'])
        result=await self._host_source_publication_original(request,current_guard=current_guard)
        if result.get('state')!='applied' or self._paired_review_ticket is not ticket or ticket.get('source_publication_fact')!=result:
            raise MemoryRequestRejectedError('source publication acknowledgment unavailable')
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        process=transport.process if transport is not None else None
        if prepared is None or process is None or ticket.get('source_publication_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode):
            raise MemoryRequestRejectedError('source publication custody unavailable')
        _,cancelled=await _completed_turn_thread(lambda: _source_publication_settled(prepared.database_path,owner=request['owner'],
            account_id=request['source']['account_id'],job_id=request['job_id'],attempt_id=request['attempt_id'],lease_epoch=request['lease_epoch']))
        if cancelled: raise asyncio.CancelledError
        if (current_guard() is not True or self._paired_review_ticket is not ticket or ticket.get('source_publication_identity')!=identity
            or ticket.get('source_publication_fact')!=result or self._guarded_preparation is not prepared
            or self._owned_transport is not transport or self._owned_generation is not generation or self._owner_task is not task
            or transport is None or not transport.current(generation) or transport.process is not process):
            raise MemoryRequestRejectedError('source publication acknowledgment changed')
        self._paired_review_ticket=None
        return result

    async def _host_paired_publication_original(self,request):
        if type(request) is not dict:raise MemoryRequestRejectedError('paired publication unavailable')
        try:
            encoded=json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(encoded.encode('utf-8'))>65536:raise ValueError('request too large')
            request=json.loads(encoded)
        except (TypeError,ValueError,OverflowError):raise MemoryRequestRejectedError('paired publication unavailable') from None
        owner=request.get('owner'); workspace=request.get('workspace_id');source=request.get('source')
        if _provider_source_mapping_error(self, source): raise MemoryRequestRejectedError('paired publication unavailable')
        if any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace)) or type(source) is not dict:
            raise MemoryRequestRejectedError('paired publication scope unavailable')
        local=self._paired_review_ticket
        if local is not None:
            prior=local.get('paired_publication_identity')
            if type(prior) is not dict or {k:v for k,v in prior.items() if k!='owner'}!={k:v for k,v in request.items() if k!='owner'}:
                raise MemoryRequestRejectedError('paired publication original identity changed')
        read_ticket={};prepared=self._guarded_preparation;current=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        current_process=current.process if current is not None else None
        if local is not None:
            if prepared is None or current_process is None or local.get('paired_publication_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode):
                raise MemoryRequestRejectedError('paired publication custody unavailable')
            try:await asyncio.to_thread(prepared.assert_valid)
            except (OSError,ValueError,sqlite3.Error) as exc:raise MemoryRequestRejectedError('paired publication custody unavailable') from exc
        result=await self._s07_owner_rpc(owner,workspace,{'phase':'paired_publication_original','request':request},asyncio.get_running_loop().time()+1.5,read_ticket=read_ticket)
        if _provider_source_mapping_error(self, source): raise MemoryRequestRejectedError('paired publication profile changed')
        if result=={'version':1,'state':'unknown'} and type(result.get('version')) is int:return result
        terminal=result=={'version':1,'state':'terminal_erased','publication':'unproved','visibility':'not_rechecked'} and type(result.get('version')) is int
        expected='s05paircandidate_'+hashlib.sha256(json.dumps([source.get('account_id'),request.get('job_id')],ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        if not terminal and (type(result) is not dict or set(result)!={'version','state','candidate_id','visibility'}
            or type(result['version']) is not int or result['version']!=1 or result['state']!='applied'
            or result['visibility']!='not_rechecked' or result['candidate_id']!=expected):
            raise MemoryRequestRejectedError('paired publication receipt unavailable')
        if local is not None:
            old=local.get('transport');old_process=local.get('paired_publication_process');settled=local.get('pending_pair_io_settled') is True
            if not settled and old is not None and old_process is not None and old.process is old_process and old.generation is local.get('generation') and old_process.returncode is not None:
                try:
                    exited=old.exit_confirmation(local['generation'])
                    settled=exited.done() and not exited.cancelled() and exited.result()==old_process.returncode
                except (RuntimeError,asyncio.CancelledError):settled=False
            try:await asyncio.to_thread(prepared.assert_valid)
            except (OSError,ValueError,sqlite3.Error) as exc:raise MemoryRequestRejectedError('paired publication custody unavailable') from exc
            if (not settled or self._paired_review_ticket is not local or local.get('paired_publication_identity')!=prior
                or self._guarded_preparation is not prepared or self._owned_transport is not current or self._owned_generation is not generation or self._owner_task is not task
                or read_ticket.get('transport') is not current or read_ticket.get('generation') is not generation or current is None or not current.current(generation)
                or current.process is not current_process or local.get('paired_publication_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
                raise MemoryRequestRejectedError('paired publication custody changed')
            local['paired_publication_fact']=copy.deepcopy(result)
        return result

    async def _ack_paired_publication(self,request,*,current_guard):
        from src.openclank.memory_job_admission import _paired_publication_settled
        try:
            if type(request) is not dict:raise ValueError('request unavailable')
            encoded=json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(encoded.encode('utf-8'))>65536:raise ValueError('request too large')
            request=json.loads(encoded)
        except (TypeError,ValueError,OverflowError):raise MemoryRequestRejectedError('paired publication acknowledgment unavailable') from None
        if not callable(current_guard) or current_guard() is not True:
            raise MemoryRequestRejectedError('paired publication acknowledgment unavailable')
        ticket=self._paired_review_ticket
        if type(ticket) is not dict or type(ticket.get('paired_publication_identity')) is not dict:
            raise MemoryRequestRejectedError('paired publication acknowledgment unavailable')
        identity=copy.deepcopy(ticket['paired_publication_identity'])
        result=await self._host_paired_publication_original(request)
        if (self._paired_review_ticket is not ticket or ticket.get('paired_publication_identity')!=identity
            or result.get('state') not in ('applied','terminal_erased') or ticket.get('paired_publication_fact')!=result):
            raise MemoryRequestRejectedError('paired publication acknowledgment unavailable')
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        if prepared is None or transport is None or ticket.get('paired_publication_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode):
            raise MemoryRequestRejectedError('paired publication acknowledgment unavailable')
        try:
            await asyncio.to_thread(prepared.assert_valid)
            if result['state']=='applied':
                await asyncio.to_thread(_paired_publication_settled,prepared.database_path,owner=request['owner'],account_id=request['source']['account_id'],job_id=request['job_id'],attempt_id=request['attempt_id'],lease_epoch=request['lease_epoch'])
            await asyncio.to_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error,KeyError) as exc:
            raise MemoryRequestRejectedError('paired publication acknowledgment unavailable') from exc
        if (current_guard() is not True or self._paired_review_ticket is not ticket or ticket.get('paired_publication_identity')!=identity or ticket.get('paired_publication_fact')!=result
            or self._guarded_preparation is not prepared or self._owned_transport is not transport or self._owned_generation is not generation
            or self._owner_task is not task or not transport.current(generation)
            or ticket.get('paired_publication_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
            raise MemoryRequestRejectedError('paired publication acknowledgment changed')
        self._paired_review_ticket=None
        return result

    async def _host_paired_erased_fact(self, projection, *, account_id, current):
        """Read a bounded terminal fact; the caller retains the real lifecycle guard."""
        if (type(projection) is not _HostPairedSourceProjection or not callable(current) or current() is not True
                or projection.source.root.account_id!=account_id or self._scope(projection.owner).owner!=projection.owner):
            raise MemoryRequestRejectedError('paired erased fact unavailable')
        from src.openclank.memory_job_admission import _exact_json
        ref=projection.source;coverage={'version':1,'kind':'assistant_interpretation_review','assistant':{'message_id':ref.assistant_message_id,'part_id':ref.assistant_part_id,'revision':ref.assistant_revision,'content_hash':ref.assistant_content_hash},'pair_sha256':ref.pair_sha256}
        source_key=hashlib.sha256(_exact_json(['assistant_interpretation_review',asdict(ref.root),coverage]).encode('utf-8')).hexdigest()
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        if prepared is None or transport is None or transport.process is None or task is None or task.done():
            raise MemoryRequestRejectedError('paired erased fact unavailable')
        process=transport.process
        def frame_current():
            try:
                return (current() is True and self._guarded_preparation is prepared and self._owned_transport is transport
                    and self._owned_generation is generation and self._owner_task is task and not task.done()
                    and transport.process is process and process.returncode is None and transport.current(generation))
            except Exception:return False
        await asyncio.to_thread(prepared.assert_valid)
        ticket={};request={'version':1,'owner':projection.owner,'account_id':account_id,'workspace_id':projection.workspace_id,'job_id':projection.job_id}
        result=await self._s07_owner_rpc(projection.owner,projection.workspace_id,{'phase':'paired_erased_fact','request':request},
            float(asyncio.get_running_loop().time()+1.5),read_ticket=ticket,_paired_current=frame_current)
        await asyncio.to_thread(prepared.assert_valid)
        if (frame_current() is not True or ticket.get('transport') is not transport or ticket.get('generation') is not generation
                or ticket.get('issued') is not True or ticket.get('frame_seen') is not True or ticket.get('frame_invalid') is not False
                or ticket.get('terminal_error') is True or type(result) is not dict
                or set(result)!={'version','state','source_key','pair_sha256','input_sha256','original_receipt_sha256'}
                or type(result.get('version')) is not int or result['version']!=1 or result['state']!='erased'
                or result['source_key']!=source_key or result['pair_sha256']!=projection.source.pair_sha256 or result['input_sha256']!=projection.input_sha256
                or any(type(result[k]) is not str or re.fullmatch(r'[0-9a-f]{64}',result[k]) is None for k in ('source_key','input_sha256','original_receipt_sha256'))):
            raise MemoryRequestRejectedError('paired erased fact unavailable')
        return result

    async def _host_pending_pair(self, request, *, phase, before_issue=None):
        if type(request) is not dict or phase not in ('pending_pair_fence','terminal_pair_fence','erased_pair_fence','pending_pair_rebind','pending_pair_original'):
            raise MemoryRequestRejectedError('pending pair request unavailable')
        try:
            encoded=json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(encoded.encode('utf-8'))>65536:raise ValueError('request too large')
            request=json.loads(encoded)
        except (TypeError,ValueError,OverflowError):
            raise MemoryRequestRejectedError('pending pair request unavailable') from None
        if _provider_source_mapping_error(self, request.get('source')):
            raise MemoryRequestRejectedError('pending pair source unavailable')
        owner=(request.get('phase') or {}).get('owner') if phase in ('pending_pair_fence','terminal_pair_fence','erased_pair_fence') else request.get('owner')
        workspace=request.get('workspace_id')
        if any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace)):
            raise MemoryRequestRejectedError('pending pair scope unavailable')
        local=self._paired_review_ticket
        if phase=='pending_pair_rebind' and local is not None:
            raise MemoryRequestRejectedError('pending pair outcome requires original status')
        if phase=='pending_pair_original' and local is not None and local.get('pending_pair_identity')!=request:
            raise MemoryRequestRejectedError('pending pair original identity changed')
        if phase=='pending_pair_original':
            read_ticket={}; prepared=self._guarded_preparation; current=self._owned_transport
            generation=self._owned_generation; task=self._owner_task; current_process=current.process if current is not None else None
            if local is not None:
                if prepared is None or current is None or current.process is None or local.get('pending_pair_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode):
                    raise MemoryRequestRejectedError('pending pair custody unavailable')
                try: await asyncio.to_thread(prepared.assert_valid)
                except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('pending pair custody unavailable') from exc
            result=await self._s07_owner_rpc(owner,workspace,{'phase':phase,'request':request},asyncio.get_running_loop().time()+1.5,read_ticket=read_ticket)
        else:
            if not callable(before_issue):
                raise MemoryRequestRejectedError('pending pair authority unavailable')
            write_status={}
            result=await self._host_candidate_request(owner,workspace,{'phase':phase,'request':request},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue,write_status=write_status)
        if _provider_source_mapping_error(self, request.get('source')):
            raise MemoryRequestRejectedError('pending pair source profile changed')
        if type(result) is not dict or type(result.get('version')) is not int or result['version']!=1:
            raise MemoryRequestRejectedError('pending pair receipt unavailable')
        if phase=='pending_pair_original' and result=={'version':1,'state':'absent'}:
            return result
        if phase in ('pending_pair_fence','terminal_pair_fence','erased_pair_fence'):
            if set(result)!={'version','state','fence_id','root','assistant'} or result['state']!='fenced' or type(result['fence_id']) is not str or re.fullmatch(r'cs_s04pairpending_[0-9a-f]{64}',result['fence_id']) is None:
                raise MemoryRequestRejectedError('pending pair receipt unavailable')
            source=request.get('source');coverage=request.get('coverage');part=coverage.get('assistant') if type(coverage) is dict else None
            if type(source) is not dict or type(part) is not dict or any(k not in source for k in ('version','account_id','chat_id','actor_id','message_id','part_id','revision','content_hash')) or any(k not in part for k in ('message_id','part_id','revision','content_hash')):
                raise MemoryRequestRejectedError('pending pair receipt unavailable')
            compact=lambda value:json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')
            root_id='hostpart_'+hashlib.sha256(compact([source[k] for k in ('version','account_id','chat_id','actor_id','message_id','part_id')])).hexdigest()
            assistant_id='hostassistant_'+hashlib.sha256(compact(['openclank.assistant-source.v1',source['account_id'],source['chat_id'],part['message_id'],part['part_id']])).hexdigest()
            for key,expected_id,revision,digest in (('root',root_id,source['revision'],source['content_hash']),('assistant',assistant_id,part['revision'],part['content_hash'])):
                item=result[key]
                if (type(item) is not dict or set(item)!={'source_id','revision','content_hash','prior_state','fence_id'}
                    or item['source_id']!=expected_id or type(item['revision']) is not int or item['revision']!=revision or item['content_hash']!=digest
                    or item['prior_state'] not in ('active','absent') or (key=='assistant' and item['prior_state']=='absent' and item['fence_id'] is not None)
                    or ((key=='root' or item['prior_state']=='active') and (type(item['fence_id']) is not str or re.fullmatch(r'cs_s04pair_[0-9a-f]{64}',item['fence_id']) is None))):
                    raise MemoryRequestRejectedError('pending pair receipt unavailable')
        elif (set(result)!={'version','state','replayed','job_id','input_sha256'} or result['state']!='rebound'
              or type(result['replayed']) is not bool or result['job_id']!=request.get('job_id')
              or type(result['input_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['input_sha256']) is None):
            raise MemoryRequestRejectedError('pending pair receipt unavailable')
        if phase=='pending_pair_original' and result.get('state')=='rebound' and local is not None:
            old=local.get('transport'); old_process=local.get('pending_pair_process')
            settled=local.get('pending_pair_io_settled') is True
            if not settled and old is not None and old_process is not None and old.process is old_process and old.generation is local.get('generation') and old_process.returncode is not None:
                try:
                    exited=old.exit_confirmation(local['generation'])
                    settled=exited.done() and not exited.cancelled() and exited.result()==old_process.returncode
                except (RuntimeError,asyncio.CancelledError):settled=False
            try: await asyncio.to_thread(prepared.assert_valid)
            except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('pending pair custody unavailable') from exc
            if (not settled or self._paired_review_ticket is not local or local.get('pending_pair_identity')!=request
                or self._guarded_preparation is not prepared or self._owned_transport is not current or self._owned_generation is not generation
                or self._owner_task is not task or read_ticket.get('transport') is not current or read_ticket.get('generation') is not generation
                or current is None or not current.current(generation) or current.process is not current_process or current_process is None
                or local.get('pending_pair_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)):
                raise MemoryRequestRejectedError('pending pair custody changed')
            self._paired_review_ticket=None
        if phase=='pending_pair_rebind':
            ticket=write_status.get('ticket')
            if ticket is None or self._paired_review_ticket is not ticket or ticket.get('pending_pair_identity')!=request or ticket.get('pending_pair_io_settled') is not True:
                raise MemoryRequestRejectedError('pending pair custody changed')
            self._paired_review_ticket=None
        return result

    async def _host_paired_rename_phase(self, request, *, renew, before_issue):
        if type(request) is not dict or not callable(before_issue):
            raise MemoryRequestRejectedError('paired rename authority unavailable')
        phase=request.get('transition' if renew else 'phase')
        owner=phase.get('new_owner' if renew else 'owner') if type(phase) is dict else None
        workspace=request.get('workspace_id')
        if any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace)):
            raise MemoryRequestRejectedError('paired rename scope unavailable')
        result=await self._host_candidate_request(owner,workspace,{'phase':'paired_rename_renew' if renew else 'paired_rename_fence','request':request},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=before_issue)
        if (type(result) is not dict or set(result)!={'version','state','root_fence_id','assistant_fence_id'}
                or type(result['version']) is not int or result['version']!=1 or result['state']!=('renewed' if renew else 'fenced')
                or any(type(result[k]) is not str or re.fullmatch(r'cs_s04pair_[0-9a-f]{64}',result[k]) is None for k in ('root_fence_id','assistant_fence_id'))):
            raise MemoryRequestRejectedError('paired rename receipt unavailable')
        return result

    async def _host_paired_rename_fence(self, request, *, before_issue):
        return await self._host_paired_rename_phase(request,renew=False,before_issue=before_issue)

    async def _host_paired_rename_renew(self, request, *, before_issue):
        return await self._host_paired_rename_phase(request,renew=True,before_issue=before_issue)

    async def _host_pair_review_apply(self, request: _HostPairedReview, *, policy_projection, refs, deadline, before_issue, write_status=None):
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        body=self._pair_review_body(request,refs)
        coordinator=policy_coordinator()
        if (type(policy_projection) is not PolicyProjection or coordinator is None or coordinator.projection is not policy_projection
            or policy_projection._preparation is not self._guarded_preparation or self._scope(request.owner).owner!=request.owner
            or request.workspace_id!=self._workspace_id or coordinator.checked_refs(request.owner,request.account_id,request.workspace_id)!=refs):
            raise MemoryRequestRejectedError('paired review policy unavailable')
        result=await self._host_candidate_request(request.owner,request.workspace_id,{'phase':'paired_review_apply','request':body},
            require_live=True,write_deadline=deadline,write_status=write_status,before_issue=before_issue)
        return self._validate_pair_review_receipt(request,result,status=False)

    def _host_pair_review_inflight(self, *, owner, workspace_id, account_id, candidate_id):
        """Project only the existing owned ticket; caller rechecks current human authority."""
        ticket=self._paired_review_ticket
        identity=ticket.get('paired_identity') if type(ticket) is dict else None
        if (type(identity) is not dict or identity.get('owner')!=owner or identity.get('workspace_id')!=workspace_id
            or identity.get('account_id')!=account_id or identity.get('candidate_id')!=candidate_id):
            return None
        original={key:identity[key] for key in ('candidate_id','candidate_digest','association_sha256','request_key','reason','action')}
        return {'state':'found','original':original,'receipt':{'state':'unknown'}}

    async def _host_pair_review_original(self, *, owner, workspace_id, account_id, candidate_id, deadline=None):
        if (not all(type(v) is str and v and v==v.strip() and len(v)<=512 for v in (owner,workspace_id,account_id))
            or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id or type(candidate_id) is not str
            or re.fullmatch(r's05paircandidate_[0-9a-f]{64}',candidate_id) is None):
            raise MemoryRequestRejectedError('paired original unavailable')
        inflight=self._host_pair_review_inflight(owner=owner,workspace_id=workspace_id,account_id=account_id,candidate_id=candidate_id)
        if inflight is not None and (self._owner_rpc_busy or self._bounded_write_reserved or self._owned_poisoned): return inflight
        try:
            payload={'phase':'paired_review_original','account_id':account_id,'candidate_id':candidate_id}
            if deadline is None:
                result=await self._host_candidate_request(owner,workspace_id,payload,require_live=True)
            else:
                import math
                if type(deadline) not in (int,float) or not math.isfinite(deadline):
                    raise MemoryRequestRejectedError('paired original unavailable')
                result=await self._s07_owner_rpc(owner,workspace_id,payload,deadline,read_ticket={})
                if result is None:
                    if inflight is not None: return inflight
                    raise MemoryRequestRejectedError('paired original unavailable')
        except MemoryRequestRejectedError:
            if inflight is not None: return inflight
            raise
        if result=={'state':'none'} and inflight is not None: return inflight
        if result=={'state':'none'}: return result
        if (type(result) is not dict or set(result)!={'state','original','receipt'} or result['state']!='found'
            or type(result['original']) is not dict or set(result['original'])!={'candidate_id','candidate_digest','association_sha256','request_key','reason','action'}):
            raise MemoryRequestRejectedError('paired original unavailable')
        original=result['original']
        if original['candidate_id']!=candidate_id: raise MemoryRequestRejectedError('paired original changed')
        review=_HostPairedReview(owner,workspace_id,account_id,candidate_id,original['candidate_digest'],original['association_sha256'],
            original['request_key'],original['reason'],original['action'])
        self._pair_review_body(review)
        self._validate_pair_review_receipt(review,result['receipt'],status=True)
        if inflight is not None:
            if inflight['original']!=original: raise MemoryRequestRejectedError('paired original conflict')
            self._paired_review_ticket=None
        return result

    async def _host_pair_original_page(self, *, owner, workspace_id, account_id, after=None, limit=32, deadline=None):
        if (not all(type(v) is str and v and v==v.strip() and len(v)<=512 for v in (owner,workspace_id,account_id))
            or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id or type(limit) is not int or not 1<=limit<=32
            or (after is not None and (type(after) is not str or re.fullmatch(r's05paircandidate_[0-9a-f]{64}',after) is None))):
            raise MemoryRequestRejectedError('paired original page unavailable')
        payload={'phase':'paired_original_page','account_id':account_id,'after':after,'limit':limit}
        if deadline is None:
            result=await self._host_candidate_request(owner,workspace_id,payload,require_live=True)
        else:
            import math
            if type(deadline) not in (int,float) or not math.isfinite(deadline): raise MemoryRequestRejectedError('paired original page unavailable')
            result=await self._s07_owner_rpc(owner,workspace_id,payload,deadline,read_ticket={})
        if (type(result) is not dict or set(result)!={'version','items','next_cursor'} or type(result['version']) is not int or result['version']!=1
            or type(result['items']) is not list or len(result['items'])>limit
            or (result['next_cursor'] is not None and (type(result['next_cursor']) is not str or re.fullmatch(r's05paircandidate_[0-9a-f]{64}',result['next_cursor']) is None))):
            raise MemoryRequestRejectedError('paired original page unavailable')
        ids=[]
        for item in result['items']:
            if (type(item) is not dict or set(item)!={'state','original','receipt'} or item['state']!='found' or type(item['original']) is not dict
                or set(item['original'])!={'candidate_id','candidate_digest','association_sha256','request_key','reason','action'}):
                raise MemoryRequestRejectedError('paired original page unavailable')
            original=item['original']; cid=original['candidate_id']
            review=_HostPairedReview(owner,workspace_id,account_id,cid,original['candidate_digest'],original['association_sha256'],original['request_key'],original['reason'],original['action'])
            self._pair_review_body(review); self._validate_pair_review_receipt(review,item['receipt'],status=True); ids.append(cid)
        cursor=result['next_cursor']
        if (any(ids[i]>=ids[i+1] for i in range(len(ids)-1)) or (after is not None and (any(v<=after for v in ids) or (cursor is not None and cursor<=after)))
            or (cursor is not None and any(v>cursor for v in ids))):
            raise MemoryRequestRejectedError('paired original page changed')
        return result

    async def _host_paired_profile_state(self) -> str:
        """Return ready/absent only for the current verified native owner."""
        prepared=self._guarded_preparation
        owner_task=self._owner_task
        ready=self._ready
        transport=self._owned_transport
        generation=self._owned_generation
        process=transport.process if transport is not None else None
        if (not self._guarded_v17 or self._broker_url or self._owned_poisoned or prepared is None
            or owner_task is None or owner_task.done() or ready is None or not ready.done()
            or ready.cancelled() or ready.exception() is not None or type(prepared.schema_cookie) is not int
            or type(prepared.capabilities.get('assistant_interpretation')) is not bool
            or (transport is not None and (process is None or not transport.current(generation)))):
            raise MemoryRequestRejectedError('paired profile unavailable')
        try:
            await asyncio.to_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('paired profile unavailable') from exc
        if (self._guarded_preparation is not prepared or self._owner_task is not owner_task or owner_task.done()
            or self._ready is not ready or self._owned_transport is not transport or self._owned_generation is not generation
            or (transport is not None and (transport.process is not process or not transport.current(generation)))
            or self._owned_poisoned or ready.cancelled() or ready.exception() is not None):
            raise MemoryRequestRejectedError('paired profile changed')
        return 'ready' if prepared.capabilities['assistant_interpretation'] else 'absent'

    async def _host_pair_root_fence(self, request: _HostPairedSourceProjection) -> Dict[str, Any]:
        """Host caller holds the original root mutation/retirement guard."""
        from src.memery_external_sources import AssistantInterpretationRef, _assistant_ref_error
        if type(request) is not _HostPairedSourceProjection or type(request.source) is not AssistantInterpretationRef or _assistant_ref_error(request.source) or _provider_source_ref_error(self, request.source.root):
            raise MemoryRequestRejectedError('paired root fence unavailable')
        root=_HostSourceProjection(request.owner,request.workspace_id,request.job_id,request.source.root,request.policy_fingerprint)
        return await self._host_source_projection(root,fence=True)

    async def _host_pair_source_fence(self, request: _HostPairedSourceProjection) -> Dict[str, Any]:
        """Host-only; caller holds the exact current mutation or retirement guard."""
        from src.memery_external_sources import AssistantInterpretationRef, _assistant_ref_error
        if (type(request) is not _HostPairedSourceProjection or type(request.source) is not AssistantInterpretationRef
            or _assistant_ref_error(request.source) or _provider_source_ref_error(self, request.source.root) or not all(type(v) is str and v and v==v.strip() and len(v)<=512
                for v in (request.owner,request.workspace_id,request.job_id))
            or re.fullmatch(r'[0-9a-f]{64}',request.policy_fingerprint or '') is None
            or self._scope(request.owner).owner!=request.owner):
            raise MemoryRequestRejectedError('paired source fence unavailable')
        ref=request.source
        coverage={'version':1,'kind':'assistant_interpretation_review','assistant':{'message_id':ref.assistant_message_id,
            'part_id':ref.assistant_part_id,'revision':ref.assistant_revision,'content_hash':ref.assistant_content_hash},'pair_sha256':ref.pair_sha256}
        body={'version':1,'owner':request.owner,'workspace_id':request.workspace_id,'job_id':request.job_id,
            'source':asdict(ref.root),'coverage':coverage,'policy_fingerprint':request.policy_fingerprint}
        result=await self._host_candidate_request(request.owner,request.workspace_id,{'phase':'paired_source_fence','request':body},require_live=True)
        if (type(result) is not dict or set(result)!={'version','state'} or type(result['version']) is not int
            or result['version']!=1 or result['state'] not in ('fenced','already_fenced','absent_fenced')):
            raise MemoryRequestRejectedError('paired source fence receipt unavailable')
        return result

    async def _host_pair_reset_io(self, request, *, claim, mapping, guard, target_authority, closure, action, write_status=None):
        """One exact paired reset step under the original retained manifest."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from services.memory.skill_lifecycle import locked
        from src.memery_external_sources import AssistantInterpretationRef, _assistant_ref_error
        from src.openclank.chat_source_bridge import _reset_ref_digest
        from src.openclank.mimo_supervisor import _queued_owned_thread
        if (type(request) is not _HostPairedSourceProjection or type(request.source) is not AssistantInterpretationRef
            or _assistant_ref_error(request.source) or _provider_source_ref_error(self, request.source.root) or type(claim) is not MemoryResetClaim or closure is None or closure.lease.claim is not claim
            or action not in ('root_write','root_read','assistant_write','assistant_read') or request.source.root.account_id!=claim.account_id
            or self._scope(request.owner).owner!=request.owner):
            raise MemoryRequestRejectedError('paired reset authority unavailable')
        ref=request.source;pool=target_authority._supervisor;digest=_reset_ref_digest(request)
        def manifest_current():
            claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
            with locked(os.fspath(claim._coordinator.lock_target)):
                row=claim._coordinator._load_journal().get(claim.operation_id)
                manifest=row.get('reset_source_manifest') if type(row) is dict else None
                records=row.get('reset_source_receipts') if type(row) is dict else None
                progress=records.get(digest) if type(records) is dict else None
                if (type(manifest) is not dict or not any(digest in root.get('refs',[]) for root in manifest.get('roots',[]) if type(root) is dict)
                    or type(progress) is not dict or progress.get('state') not in (('started',) if action.endswith('write') else ('started','complete'))):
                    raise MemoryRequestRejectedError('paired reset manifest unavailable')
        async def current():
            mapping.validate_chat_guard(ref.root.chat_id,guard)
            await pool.validate_queued_reset_closure(closure,claim=claim)
            await _queued_owned_thread(self._assert_reset_retirement_ready,closure.lease.reset_transport_witness)
            binding=target_authority._resolve_stopped_reset(claim.account_id,closure=closure,claim=claim)
            if binding.memory_owner!=request.owner or binding.memory_workspace!=request.workspace_id:
                raise MemoryRequestRejectedError('paired reset target changed')
            await asyncio.to_thread(manifest_current)
            return binding
        before=await current();reset=await asyncio.to_thread(self._reset_fence_binding,claim)
        if 'memories' not in reset.get('components',[]): raise MemoryRequestRejectedError('paired reset requires memories')
        root=_HostSourceProjection(request.owner,request.workspace_id,request.job_id,ref.root,request.policy_fingerprint)
        if action=='root_write':
            result=await self._host_source_fence_inner(root,reset_claim=claim,mapping=mapping,guard=guard,target_authority=target_authority,reset_closure=closure,write_status=write_status)
        elif action=='root_read':
            result=await self._read_reset_source_fence(root,claim=claim,mapping=mapping,guard=guard,target_authority=target_authority,closure=closure)
        else:
            coverage={'version':1,'kind':'assistant_interpretation_review','assistant':{'message_id':ref.assistant_message_id,'part_id':ref.assistant_part_id,'revision':ref.assistant_revision,'content_hash':ref.assistant_content_hash},'pair_sha256':ref.pair_sha256}
            body={'version':1,'owner':request.owner,'workspace_id':request.workspace_id,'job_id':request.job_id,'source':asdict(ref.root),'coverage':coverage,'policy_fingerprint':request.policy_fingerprint}
            if action=='assistant_write':
                result=await self._host_candidate_request(request.owner,request.workspace_id,{'phase':'paired_reset_fence','request':body,'reset':reset},require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),write_status=write_status,reset_feature=closure.lease.reset_transport_witness,terminal_feature=(claim._terminal_s08_witness if reset.get('mode')=='terminal-s08-v1' else None))
            else:
                result=await self._s07_owner_rpc(request.owner,request.workspace_id,{'phase':'paired_reset_fence_read','request':body,'reset':reset},asyncio.get_running_loop().time()+6.0,read_ticket={})
            if (type(result) is not dict or set(result)!={'version','state'} or type(result['version']) is not int or result['version']!=1
                or result['state'] not in (('fenced','already_fenced','absent_fenced') if action=='assistant_write' else ('already_fenced','absent_fenced'))):
                raise MemoryRequestRejectedError('paired reset source outcome unavailable')
        if await current()!=before or await asyncio.to_thread(self._reset_fence_binding,claim)!=reset:
            raise MemoryRequestRejectedError('paired reset authority changed')
        return result

    def _host_source_refs(self, *, account_id: str, chat_id: str, message_id: str, target_authority, limit: int = 128, after_job_id=None, reset_claim=None, reset_closure=None, reset_lease=None, _erased_token=None):
        if not isinstance(message_id, str) or not message_id.strip():
            raise MemoryRequestRejectedError('host source inventory unavailable')
        return self._host_source_inventory(account_id=account_id, chat_id=chat_id, message_id=message_id,
                                           target_authority=target_authority, limit=limit, after_job_id=after_job_id,
                                           reset_claim=reset_claim, reset_closure=reset_closure, reset_lease=reset_lease, _erased_token=_erased_token)

    def _host_chat_source_refs(self, *, account_id: str, chat_id: str, target_authority, limit: int = 128, after_job_id=None, _erased_token=None):
        """Enumerate every scoped chat source, including deleted host messages."""
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise MemoryRequestRejectedError('host source inventory unavailable')
        return self._host_source_inventory(account_id=account_id, chat_id=chat_id, message_id=None,
                                           target_authority=target_authority, limit=limit, after_job_id=after_job_id, _erased_token=_erased_token)

    def _host_account_source_refs(self, *, account_id: str, target_authority, limit: int = 128, after_job_id=None, claimed_phase=None, phase_capability=None, _erased_token=None):
        """Enumerate all scoped account sources, including deleted chats."""
        return self._host_source_inventory(account_id=account_id, chat_id=None, message_id=None,
                                           target_authority=target_authority, limit=limit, after_job_id=after_job_id,
                                           claimed_phase=claimed_phase,phase_capability=phase_capability, _erased_token=_erased_token)

    def _host_observation_source_page(self, db, *, account_id, chat_id, message_id, target, sql_owner, limit, after, deadline):
        from src.memery_external_sources import SourceRef, _source_ref_error
        from src.openclank.memory_job_admission import _hash
        import re
        from src.openclank.owner_custody_facts import verified_owner_custody_phase_context
        import time
        database=db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
        if database is None:raise ValueError('observation custody unavailable')
        custody_owners,phase_locate,legacy_locate,owner_locate=verified_owner_custody_phase_context(db,database_id=database[0],account_id=account_id,current_owner=sql_owner)
        if db.execute("SELECT 1 FROM fm_v2_change_sets INDEXED BY s06_observation_origin_invalid WHERE owner_id=? AND reason='s06.observation-origin.v1' AND NOT json_valid(operation_metadata_json) LIMIT 1",(sql_owner,)).fetchone():
            raise ValueError('invalid observation origin')
        if db.execute("SELECT 1 FROM fm_v2_change_sets INDEXED BY s06_observation_rebind_invalid_json WHERE owner_id=? AND reason='s06.observation-rebind.v2' AND NOT json_valid(operation_metadata_json) LIMIT 1",(sql_owner,)).fetchone():
            raise ValueError('invalid observation rebind')
        bad_rebind = "json_type(operation_metadata_json, '$.origin_id') IS NOT 'text'\n       OR length(json_extract(operation_metadata_json, '$.origin_id')) != 77\n       OR substr(json_extract(operation_metadata_json, '$.origin_id'),1,13) != 'cs_s06origin_'\n       OR substr(json_extract(operation_metadata_json, '$.origin_id'),14) GLOB '*[^0-9a-f]*'\n       OR json_type(operation_metadata_json, '$.current_fingerprint') IS NOT 'text'\n       OR length(json_extract(operation_metadata_json, '$.current_fingerprint')) != 64\n       OR json_extract(operation_metadata_json, '$.current_fingerprint') GLOB '*[^0-9a-f]*'\n       OR (json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT 'null' AND json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT 'text')\n       OR (json_type(operation_metadata_json, '$.prior_receipt_hash') IS NOT 'null' AND json_type(operation_metadata_json, '$.prior_receipt_hash') IS NOT 'text')\n       OR (json_type(operation_metadata_json, '$.prior_receipt_id') IS NOT json_type(operation_metadata_json, '$.prior_receipt_hash'))\n       OR (json_type(operation_metadata_json, '$.prior_receipt_id') = 'text' AND (length(json_extract(operation_metadata_json, '$.prior_receipt_id')) != 76\n          OR substr(json_extract(operation_metadata_json, '$.prior_receipt_id'),1,12) != 'cs_s06renew_'\n          OR substr(json_extract(operation_metadata_json, '$.prior_receipt_id'),13) GLOB '*[^0-9a-f]*'\n          OR length(json_extract(operation_metadata_json, '$.prior_receipt_hash')) != 64\n          OR json_extract(operation_metadata_json, '$.prior_receipt_hash') GLOB '*[^0-9a-f]*'))"
        if db.execute(f"SELECT 1 FROM fm_v2_change_sets INDEXED BY s06_observation_rebind_invalid_fields WHERE owner_id=? AND reason='s06.observation-rebind.v2' AND json_valid(operation_metadata_json) AND ({bad_rebind}) LIMIT 1",(sql_owner,)).fetchone():
            raise ValueError('invalid observation rebind fields')
        invalid = "json_type(operation_metadata_json, '$.source.account_id') IS NOT 'text' OR length(json_extract(operation_metadata_json, '$.source.account_id')) > 512 OR trim(json_extract(operation_metadata_json, '$.source.account_id')) != json_extract(operation_metadata_json, '$.source.account_id') OR json_extract(operation_metadata_json, '$.source.account_id') = ''"
        if db.execute(f"SELECT 1 FROM fm_v2_change_sets INDEXED BY s06_observation_origin_invalid_account WHERE owner_id=? AND reason='s06.observation-origin.v1' AND json_valid(operation_metadata_json) AND ({invalid}) LIMIT 1",(sql_owner,)).fetchone():
            raise ValueError('invalid observation account')
        for op in ('<','>'):
            if db.execute(f"SELECT 1 FROM fm_v2_change_sets INDEXED BY s06_observation_origin_owner_account WHERE owner_id=? AND reason='s06.observation-origin.v1' AND json_valid(operation_metadata_json) AND json_extract(operation_metadata_json, '$.source.account_id') {op} ? LIMIT 1",(sql_owner,account_id)).fetchone():
                raise ValueError('observation account conflict')
        fields = ("json_extract(operation_metadata_json,'$.source.account_id')", "json_extract(operation_metadata_json,'$.source.chat_id')", "json_extract(operation_metadata_json,'$.source.message_id')")
        idx = 's06_observation_origin_account_chat_message' if message_id is not None else 's06_observation_origin_account_chat' if chat_id is not None else 's06_observation_origin_account'
        predicates = [fields[0]+'=?']; args = [account_id]
        if chat_id is not None: predicates.append(fields[1]+'=?'); args.append(chat_id)
        if message_id is not None: predicates.append(fields[2]+'=?'); args.append(message_id)
        # A matching account under a foreign memory owner is not silently skipped.
        for op in ('<','>'):
            sql = f"SELECT 1 FROM fm_v2_change_sets INDEXED BY {idx} WHERE reason='s06.observation-origin.v1' AND json_valid(operation_metadata_json) AND {' AND '.join(predicates)} AND owner_id {op} ? LIMIT 1"
            if db.execute(sql,(*args,sql_owner)).fetchone(): raise ValueError('observation owner conflict')
        sql = f"SELECT owner_id,change_set_id,content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=16384 THEN operation_metadata_json END AS operation_metadata_json FROM fm_v2_change_sets INDEXED BY {idx} WHERE reason='s06.observation-origin.v1' AND json_valid(operation_metadata_json) AND {' AND '.join(predicates)} AND owner_id=? AND change_set_id>? ORDER BY change_set_id LIMIT ?"
        rows = db.execute(sql,(*args,sql_owner,after,limit+1)).fetchall()
        result = []
        for row in rows[:limit]:
            raw = row['operation_metadata_json']
            if not isinstance(raw,str) or hashlib.sha256(raw.encode('utf-8')).hexdigest() != row['content_hash']: raise ValueError('observation hash mismatch')
            def unique(pairs):
                out = {}
                for key,value in pairs:
                    if key in out: raise ValueError('duplicate observation key')
                    out[key] = value
                return out
            obj = json.loads(raw,object_pairs_hook=unique)
            if type(obj) is not dict or set(obj) != {'version','owner','workspace_id','source_id','source'} or obj['version'] != 's06.observation-origin.v1': raise ValueError('invalid observation origin')
            ref = SourceRef(**obj['source'])
            if (_provider_source_ref_error(self, ref) or ref.account_id != account_id or (chat_id is not None and ref.chat_id != chat_id)
                or (message_id is not None and ref.message_id != message_id) or type(obj['owner']) is not str or not obj['owner']
                or obj['workspace_id'] != target.memory_workspace or row['owner_id'] != sql_owner): raise ValueError('observation scope changed')
            part = json.dumps([ref.version,ref.account_id,ref.chat_id,ref.actor_id,ref.message_id,ref.part_id],ensure_ascii=False,separators=(',',':')).encode()
            source_id = 'hostpart_' + hashlib.sha256(part).hexdigest()
            material = json.dumps([obj['owner'],target.memory_workspace,asdict(ref)],ensure_ascii=False,separators=(',',':')).encode()
            origin_id = 'cs_s06origin_' + hashlib.sha256(material).hexdigest()
            if obj['source_id'] != source_id or row['change_set_id'] != origin_id: raise ValueError('observation identity mismatch')
            outbox = db.execute("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=16384 THEN payload_json END AS payload_json,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='s06_observation_origin'",(sql_owner,'outbox_'+origin_id+'_s06_observation_origin',origin_id)).fetchone()
            if not outbox or outbox['payload_json'] != raw or outbox['source_revision'] != ref.revision: raise ValueError('observation outbox mismatch')
            count = db.execute("SELECT count(*) FROM fm_v2_change_sets INDEXED BY s06_observation_rebind_current WHERE owner_id=? AND reason='s06.observation-rebind.v2' AND json_valid(operation_metadata_json) AND json_extract(operation_metadata_json,'$.origin_id')=?",(sql_owner,origin_id)).fetchone()[0]
            heads = db.execute("""SELECT a.change_set_id,a.content_hash,CASE WHEN length(CAST(a.operation_metadata_json AS BLOB))<=65536 THEN a.operation_metadata_json END AS operation_metadata_json FROM fm_v2_change_sets a INDEXED BY s06_observation_rebind_current
                WHERE a.owner_id=? AND a.reason='s06.observation-rebind.v2' AND json_valid(a.operation_metadata_json) AND json_extract(a.operation_metadata_json,'$.origin_id')=?
                AND NOT EXISTS (SELECT 1 FROM fm_v2_change_sets b INDEXED BY s06_observation_rebind_successor WHERE b.owner_id=a.owner_id AND b.reason='s06.observation-rebind.v2' AND json_valid(b.operation_metadata_json) AND json_extract(b.operation_metadata_json,'$.origin_id')=? AND json_extract(b.operation_metadata_json,'$.prior_receipt_id')=a.change_set_id) LIMIT 2""",(sql_owner,origin_id,origin_id)).fetchall()
            if count == 0:
                if heads or obj['owner'] not in custody_owners: raise ValueError('observation rebind unavailable')
                rebind_id = rebind_hash = None
                historical_fingerprint = target.fingerprint
            else:
                if len(heads)!=1 or count>100000:raise ValueError('ambiguous observation rebind head')
                rebind_id,rebind_hash=heads[0]['change_set_id'],heads[0]['content_hash']
                fields=('database_id','account_id','operation_id','step','generation','action','owner','new_owner')
                base={'version','origin_id','origin_hash','captured_owner','prior_receipt_id','prior_receipt_hash','previous_owner','owner','workspace_id','account_id','source_id','revision','content_hash','operation_id','fence_proof_id','previous_fingerprint','current_fingerprint'}
                used=[0];seen=set();current_id=rebind_id;expected_hash=rebind_hash;newer=None;historical_fingerprint=None;next_custody_index=None
                def compact(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
                def digest(value):return hashlib.sha256(compact(value)).hexdigest()
                def charge(raw):
                    if type(raw) is not str:raise ValueError('observation receipt unavailable')
                    length=len(raw.encode('utf-8'));used[0]+=length
                    if length>65536 or used[0]>16*1024*1024 or time.monotonic()>=deadline:raise ValueError('observation receipt budget exceeded')
                def phase_fact(identifier,reason,kind,expected):
                    item=db.execute("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END AS raw,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason=?",(sql_owner,identifier,reason)).fetchone()
                    if item is None:raise ValueError('observation phase proof unavailable')
                    raw=item['raw'];charge(raw)
                    if (hashlib.sha256(raw.encode('utf-8')).hexdigest()!=item['content_hash'] or item['actor_type']!='automation' or item['actor_id']!='account_transition'
                        or item['contract_version']!='frankenmemory.v2' or item['source_event_id'] is not None or json.loads(raw,object_pairs_hook=unique)!=expected):
                        raise ValueError('observation phase proof changed')
                    event=db.execute("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END AS raw,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind=?",(sql_owner,'outbox_'+identifier+'_'+kind,identifier,kind)).fetchone()
                    if event is None:raise ValueError('observation phase outbox unavailable')
                    charge(event['raw'])
                    if event['raw']!=raw or event['source_revision']!=ref.revision:raise ValueError('observation phase outbox changed')
                while current_id is not None:
                    if current_id in seen or len(seen)>=count:raise ValueError('observation rebind chain unavailable')
                    seen.add(current_id)
                    item=db.execute("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=65536 THEN operation_metadata_json END AS raw,actor_type,actor_id,contract_version,source_event_id FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=? AND reason='s06.observation-rebind.v2'",(sql_owner,current_id)).fetchone()
                    if item is None or item['content_hash']!=expected_hash:raise ValueError('observation rebind unavailable')
                    raw=item['raw'];charge(raw)
                    if hashlib.sha256(raw.encode('utf-8')).hexdigest()!=expected_hash:raise ValueError('observation rebind hash mismatch')
                    reb=json.loads(raw,object_pairs_hook=unique)
                    if type(reb) is not dict:raise ValueError('observation rebind unavailable')
                    version=reb.get('version');v3=version=='s06.observation-rebind.v3'
                    if (set(reb)!=(base|({'phase_lineage'} if v3 else set())) or (not v3 and version!='s06.observation-rebind.v2')
                        or reb['origin_id']!=origin_id or reb['origin_hash']!=row['content_hash'] or reb['captured_owner']!=obj['owner']
                        or reb['owner'] not in custody_owners or reb['previous_owner'] not in custody_owners or reb['workspace_id']!=target.memory_workspace
                        or reb['account_id']!=account_id or reb['source_id']!=source_id or type(reb['revision']) is not int or reb['revision']!=ref.revision
                        or reb['content_hash']!=ref.content_hash or not _hash(reb['current_fingerprint']) or not _hash(reb['previous_fingerprint'])
                        or type(reb['operation_id']) is not str or not reb['operation_id'] or reb['operation_id']!=reb['operation_id'].strip()
                        or (reb['prior_receipt_id'] is None)!=(reb['prior_receipt_hash'] is None)
                        or (reb['prior_receipt_id'] is not None and (type(reb['prior_receipt_id']) is not str or not re.fullmatch(r'cs_s06renew_[0-9a-f]{64}',reb['prior_receipt_id']) or not _hash(reb['prior_receipt_hash'])))):
                        raise ValueError('observation rebind scope changed')
                    if newer is not None and (newer['previous_owner']!=reb['owner'] or newer['previous_fingerprint']!=reb['current_fingerprint']):raise ValueError('observation rebind predecessor changed')
                    if historical_fingerprint is None:historical_fingerprint=reb['current_fingerprint']
                    if v3:
                        lineage=reb['phase_lineage']
                        if type(lineage) is not dict or set(lineage)!={'transitions','root_renewal_id'} or type(lineage['transitions']) is not list or not lineage['transitions']:
                            raise ValueError('observation phase lineage unavailable')
                        segment=[]
                        for link in lineage['transitions']:
                            if type(link) is not dict or set(link)!={'phase','request_sha256','receipt_id','receipt_hash'} or type(link['phase']) is not dict or set(link['phase'])!=set(fields):raise ValueError('observation phase lineage changed')
                            phase=link['phase']
                            if (type(phase['generation']) is not int or phase['generation']<1 or phase['action'] not in ('forward','reverse')
                                or any(type(phase[k]) is not str or not phase[k] or phase[k]!=phase[k].strip() for k in fields if k not in ('generation','action'))
                                or not _hash(link['request_sha256']) or type(link['receipt_id']) is not str or not re.fullmatch(r'owner_transition_[0-9a-f]{64}',link['receipt_id']) or not _hash(link['receipt_hash'])):raise ValueError('observation phase lineage changed')
                            segment.append({'phase':{key:phase[key] for key in fields},'request_sha256':link['request_sha256'],'receipt_id':link['receipt_id'],'receipt_hash':link['receipt_hash']})
                        position=phase_locate(segment)
                        if (position is None or (next_custody_index is not None and position!=next_custody_index)
                            or segment[0]['phase']['new_owner']!=reb['owner'] or segment[-1]['phase']['owner']!=reb['previous_owner'] or segment[0]['phase']['operation_id']!=reb['operation_id']):
                            raise ValueError('observation phase lineage changed')
                        next_custody_index=position+len(segment)
                        canonical={'transitions':segment,'root_renewal_id':lineage['root_renewal_id']};transition=segment[0]['phase']
                        if current_id!='cs_s06renew_'+digest(['s06.observation-rebind-phase.v3',account_id,origin_id,canonical,source_id,ref.revision,ref.content_hash]):raise ValueError('observation phase identity changed')
                        proof='cs_s04pair_'+digest(['s04.paired-rename-fence.v1',transition,'root',source_id,None]);renewal='cs_s04pairrenew_'+digest(['s04.paired-rename-renew.v1',transition,'root',source_id,None])
                        if reb['fence_proof_id']!=proof or lineage['root_renewal_id']!=renewal:raise ValueError('observation phase proof changed')
                        phase_fact(proof,'s04.paired-rename-fence.v1','paired_rename_root_fence',{'version':'s04.paired-rename-fence.v1','phase':transition,'kind':'root','workspace_id':target.memory_workspace,'source_id':source_id,'revision':ref.revision,'content_hash':ref.content_hash})
                        phase_fact(renewal,'s04.paired-rename-renew.v1','paired_rename_root_renewed',{'version':'s04.paired-rename-renew.v1','phase':transition,'owner':reb['owner'],'kind':'root','workspace_id':target.memory_workspace,'source_id':source_id,'revision':ref.revision,'content_hash':ref.content_hash,'fence_id':proof})
                        if item['actor_type']!='automation' or item['actor_id']!='account_transition' or item['contract_version']!='frankenmemory.v2' or item['source_event_id']!=source_id:raise ValueError('observation rebind actor changed')
                    else:
                        position=legacy_locate(account_id,reb['operation_id'],reb['previous_owner'],reb['owner'])
                        if position is None or (next_custody_index is not None and position!=next_custody_index):raise ValueError('observation legacy custody unavailable')
                        next_custody_index=position+1
                        if current_id!='cs_s06renew_'+digest([account_id,reb['operation_id'],source_id,ref.revision,ref.content_hash]) or reb['fence_proof_id']!='cs_s04rename_'+digest([account_id,reb['operation_id'],source_id]):raise ValueError('observation rebind identity changed')
                        fence=db.execute("SELECT content_hash,CASE WHEN length(CAST(operation_metadata_json AS BLOB))<=16384 THEN operation_metadata_json END AS raw FROM fm_v2_change_sets WHERE owner_id=? AND change_set_id=?",(sql_owner,reb['fence_proof_id'])).fetchone()
                        if fence is None:raise ValueError('observation legacy fence unavailable')
                        charge(fence['raw'])
                        if hashlib.sha256(fence['raw'].encode('utf-8')).hexdigest()!=fence['content_hash']:raise ValueError('observation legacy fence changed')
                        old=json.loads(fence['raw'],object_pairs_hook=unique)
                        expected_fence={'version':'s04.rename-source-fence.v1','account_id':account_id,'operation_id':reb['operation_id'],'original_owner':reb['previous_owner'],'workspace':target.memory_workspace,'source_id':source_id,'active_versions':[{'revision':ref.revision,'content_hash':ref.content_hash}]}
                        if old!=expected_fence:raise ValueError('observation legacy fence changed')
                    out=db.execute("SELECT CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json END AS raw,source_revision FROM fm_v2_outbox WHERE owner_id=? AND event_id=? AND change_set_id=? AND kind='s06_observation_rebound'",(sql_owner,'outbox_'+current_id+'_s06_observation_rebound',current_id)).fetchone()
                    if out is None:raise ValueError('observation rebind outbox unavailable')
                    charge(out['raw'])
                    if json.loads(out['raw'],object_pairs_hook=unique)!=reb or out['source_revision']!=ref.revision:raise ValueError('observation rebind outbox changed')
                    if not v3 and out['raw']!=json.dumps(reb,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False):raise ValueError('observation legacy outbox changed')
                    if reb['prior_receipt_id'] is None:
                        if reb['previous_owner']!=obj['owner']:raise ValueError('observation first rebind owner mismatch')
                        if v3:
                            anchor=owner_locate(obj['owner'])
                            if anchor is None or next_custody_index!=anchor+1:raise ValueError('observation origin anchor unavailable')
                    newer=reb;current_id=reb['prior_receipt_id'];expected_hash=reb['prior_receipt_hash']
                if len(seen)!=count:raise ValueError('observation rebind chain unavailable')
            result.append(_HostObservationProjection(sql_owner,target.memory_workspace,origin_id,ref,obj['owner'],row['content_hash'],historical_fingerprint,rebind_id,rebind_hash))
        return tuple(result), ('o:'+rows[limit-1]['change_set_id'] if len(rows)>limit else None)

    def _host_pair_assistant_refs(self, *, account_id, chat_id, message_id, target_authority, limit=128, after_job_id=None, _erased_token=None):
        """Read exact assistant contributors; selectors alone confer no retirement authority."""
        from src.memery_external_sources import SourceRef, AssistantInterpretationRef, _source_ref_error, _assistant_ref_error
        from src.openclank.memory_job_admission import TargetAuthority, _conn, _exact_json, _hash, _id
        if (type(target_authority) is not TargetAuthority or not all(_id(v) for v in (account_id, chat_id, message_id))
            or (after_job_id is not None and not _id(after_job_id)) or type(limit) is not int or not 1 <= limit <= 128
            or not self._guarded_v17 or self._broker_url):
            raise MemoryRequestRejectedError('paired source inventory unavailable')
        target = target_authority.resolve(account_id)
        if target.memory_workspace != self._workspace_id or self._scope(target.memory_owner).owner != target.memory_owner:
            raise MemoryRequestRejectedError('paired source inventory scope changed')
        path = self._local_db_path()
        if not path:
            raise MemoryRequestRejectedError('paired source inventory unavailable')
        try:
            with _conn(path) as db:
                marker = db.execute("SELECT value FROM fm_meta WHERE key='openclank.assistant_interpretation.v1'").fetchone()
                expected = "CREATE INDEX s04_pair_assistant_origin ON fm_v2_s05_job_refs(source_account_id,json_extract(source_ref_json,'$.chat_id'),json_extract(coverage_json,'$.assistant.message_id'),owner_id,job_id) WHERE json_valid(source_ref_json) AND json_valid(coverage_json) AND json_extract(coverage_json,'$.kind')='assistant_interpretation_review'"
                index = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='s04_pair_assistant_origin'").fetchone()
                if marker is None or marker[0] != '1' or index is None or _canonical_index_sql(index[0]) != _canonical_index_sql(expected):
                    raise ValueError('paired profile unavailable')
                prefix = "r.source_account_id=? AND json_valid(r.source_ref_json) AND json_valid(r.coverage_json) AND json_extract(r.source_ref_json,'$.chat_id')=? AND json_extract(r.coverage_json,'$.assistant.message_id')=? AND json_extract(r.coverage_json,'$.kind')='assistant_interpretation_review'"
                if db.execute(f"SELECT 1 FROM fm_v2_s05_job_refs r INDEXED BY s04_pair_assistant_origin WHERE {prefix} AND r.owner_id!=? LIMIT 1",(account_id,chat_id,message_id,target.memory_owner)).fetchone():
                    raise ValueError('paired source owner changed')
                bad = db.execute("SELECT 1 FROM fm_v2_s05_job_refs r INDEXED BY s04_s05_source_account JOIN fm_v2_jobs j USING(owner_id,job_id) WHERE r.source_account_id=? AND r.owner_id=? AND j.kind='assistant_interpretation_review' AND (NOT json_valid(r.source_ref_json) OR NOT json_valid(r.coverage_json)) LIMIT 1",(account_id,target.memory_owner)).fetchone()
                if bad:
                    raise ValueError('paired source malformed')
                rows = db.execute(f"SELECT r.*,j.kind,j.state AS job_state,j.current_attempt_id,j.attempt_count,j.input_hash FROM fm_v2_s05_job_refs r INDEXED BY s04_pair_assistant_origin LEFT JOIN fm_v2_jobs j USING(owner_id,job_id) WHERE {prefix} AND r.owner_id=? AND (? IS NULL OR r.job_id>?) ORDER BY r.job_id LIMIT ?",(account_id,chat_id,message_id,target.memory_owner,after_job_id,after_job_id,limit+1)).fetchall()
                more = len(rows)>limit
                rows = rows[:limit]
                result=[]
                for row in rows:
                    raw=json.loads(row['source_ref_json']); coverage=json.loads(row['coverage_json'])
                    root=SourceRef(**raw)
                    if (type(coverage) is not dict or set(coverage)!={'version','kind','assistant','pair_sha256'}
                        or type(coverage.get('version')) is not int or coverage['version']!=1 or coverage['kind']!='assistant_interpretation_review'
                        or type(coverage['assistant']) is not dict or set(coverage['assistant'])!={'message_id','part_id','revision','content_hash'}):
                        raise ValueError('paired coverage unavailable')
                    a=coverage['assistant']
                    ref=AssistantInterpretationRef(1,root,a['message_id'],a['part_id'],a['revision'],a['content_hash'],coverage['pair_sha256'])
                    key=hashlib.sha256(_exact_json(['assistant_interpretation_review',asdict(root),coverage]).encode('utf-8')).hexdigest()
                    if (_provider_source_ref_error(self, root) or _assistant_ref_error(ref) or row['kind']!='assistant_interpretation_review'
                        or root.account_id!=account_id or root.chat_id!=chat_id or ref.assistant_message_id!=message_id
                        or row['source_workspace_id']!=root.workspace_id or row['target_workspace_id']!=target.memory_workspace
                        or row['source_hash']!=root.content_hash or row['source_key']!=key or not _hash(row['policy_fingerprint'])
                        or row['source_ref_json']!=json.dumps(asdict(root),sort_keys=True,separators=(',',':'))
                        or row['coverage_json']!=_exact_json(coverage)):
                        raise ValueError('paired source changed')
                    if row['job_state']=='cancelled' and row['current_attempt_id'] is None and row['attempt_count']==0:
                        if _erased_token is not _HOST_ERASED_INVENTORY or not _hash(row['input_hash']):
                            raise ValueError('paired erased source requires verified inventory')
                        projection=_HostPairedSourceProjection(target.memory_owner,target.memory_workspace,row['job_id'],ref,row['policy_fingerprint'],None,row['input_hash'])
                        result.append(_HostPairedErasedCandidate(projection))
                    else:
                        result.append(_HostPairedSourceProjection(target.memory_owner,target.memory_workspace,row['job_id'],ref,row['policy_fingerprint']))
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('paired source inventory unavailable') from exc
        if target_authority.resolve(account_id)!=target:
            raise MemoryRequestRejectedError('paired source lifecycle changed')
        return tuple(result), (rows[-1]['job_id'] if more else None)

    def _host_source_inventory(self, *, account_id, chat_id, message_id, target_authority, limit, after_job_id, reset_claim=None, reset_closure=None, reset_lease=None, claimed_phase=None, phase_capability=None, _erased_token=None):
        """Exhaustive bounded exact-part job refs under current host lifecycle."""
        from src.memery_external_sources import SourceRef, AssistantInterpretationRef, _assistant_ref_error
        from src.openclank.memory_job_admission import TargetAuthority, _conn, _exact_json, _hash, _id
        if (type(target_authority) is not TargetAuthority or not _id(account_id) or (chat_id is not None and not _id(chat_id))
            or (message_id is not None and not _id(message_id))
            or (after_job_id is not None and (type(after_job_id) is not str or not after_job_id or len(after_job_id)>514))
            or type(limit) is not int or not 1 <= limit <= 128 or not self._guarded_v17 or self._broker_url):
            raise MemoryRequestRejectedError('host source inventory unavailable')
        phase = 'o' if isinstance(after_job_id,str) and after_job_id.startswith('o:') else 'j'
        if phase == 'o':
            cursor = after_job_id[2:]
        elif isinstance(after_job_id,str) and after_job_id.startswith('j:'):
            cursor = after_job_id[2:]
        else:
            cursor = after_job_id
        if cursor is not None and cursor != '' and not _id(cursor):
            raise MemoryRequestRejectedError('host source cursor unavailable')
        path = self._local_db_path()
        if not path:
            raise MemoryRequestRejectedError('host source inventory unavailable')
        if (reset_closure is not None and reset_lease is not None or
            (reset_claim is None)!=(reset_closure is None and reset_lease is None)):
            raise MemoryRequestRejectedError('host reset source unavailable')
        def resolve():
            if reset_lease is not None:
                return target_authority._resolve_queued_reset(account_id,lease=reset_lease,claim=reset_claim)
            return target_authority.resolve(account_id) if reset_closure is None else target_authority._resolve_stopped_reset(account_id,closure=reset_closure,claim=reset_claim)
        target = resolve()
        if (claimed_phase is None)!=(phase_capability is None) or (claimed_phase is not None and (reset_claim is not None or chat_id is not None or message_id is not None)):
            raise MemoryRequestRejectedError('claimed source inventory unavailable')
        sql_owner=target.memory_owner
        if claimed_phase is not None:
            from src.openclank.chat_source_bridge import HostAccountSourceTransition
            if type(phase_capability) is not HostAccountSourceTransition or type(claimed_phase) is not dict or type(claimed_phase.get('owner')) is not str:
                raise MemoryRequestRejectedError('claimed source inventory unavailable')
            sql_owner=claimed_phase['owner']
            phase_capability.assert_prefence_current(provider=self,phase=claimed_phase,account_id=account_id,sql_owner=sql_owner)
        if target.memory_workspace != self._workspace_id or self._scope(target.memory_owner).owner != target.memory_owner:
            raise MemoryRequestRejectedError('host source inventory scope changed')
        try:
            with _conn(path) as db:
                import time
                deadline=time.monotonic()+5.0
                db.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
                db.execute('BEGIN')
                definitions = {
                    's04_s05_invalid_account': 'CREATE INDEX s04_s05_invalid_account ON fm_v2_s05_job_refs(source_account_id) WHERE NOT json_valid(source_ref_json)',
                    's04_s05_invalid_owner': 'CREATE INDEX s04_s05_invalid_owner ON fm_v2_s05_job_refs(owner_id) WHERE NOT json_valid(source_ref_json)',
                    's04_s05_ref_account': "CREATE INDEX s04_s05_ref_account ON fm_v2_s05_job_refs(json_extract(source_ref_json,'$.account_id'),owner_id,job_id) WHERE json_valid(source_ref_json)",
                    's04_s05_source_account': 'CREATE INDEX s04_s05_source_account ON fm_v2_s05_job_refs(source_account_id,owner_id,job_id)',
                }
                stored = {row['name']:row['sql'] for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND name IN (?,?,?,?)", tuple(definitions))}
                canonical = _canonical_index_sql
                if any(canonical(stored.get(name)) != canonical(sql) for name,sql in definitions.items()):
                    raise ValueError('source inventory indexes unavailable')
                feedback = self._s06_source_capability(db)
                if phase == 'o':
                    if not feedback: raise ValueError('observation capability unavailable')
                    result, continuation = self._host_observation_source_page(db,account_id=account_id,chat_id=chat_id,message_id=message_id,target=target,sql_owner=sql_owner,limit=limit,after=cursor or '',deadline=deadline)
                    if resolve() != target: raise ValueError('source lifecycle changed')
                    if phase_capability is not None:phase_capability.assert_prefence_current(provider=self,phase=claimed_phase,account_id=account_id,sql_owner=sql_owner)
                    return result, continuation
                if (db.execute('SELECT 1 FROM fm_v2_s05_job_refs INDEXED BY s04_s05_invalid_account WHERE source_account_id=? AND NOT json_valid(source_ref_json) LIMIT 1',(account_id,)).fetchone()
                    or db.execute('SELECT 1 FROM fm_v2_s05_job_refs INDEXED BY s04_s05_invalid_owner WHERE owner_id=? AND NOT json_valid(source_ref_json) LIMIT 1',(sql_owner,)).fetchone()):
                    raise ValueError('invalid source reference')
                for index,account_expr in (('s04_s05_source_account','source_account_id'),('s04_s05_ref_account',"json_extract(source_ref_json,'$.account_id')")):
                    for comparison in ('<','>'):
                        if db.execute(f'SELECT 1 FROM fm_v2_s05_job_refs INDEXED BY {index} WHERE json_valid(source_ref_json) AND {account_expr}=? AND owner_id {comparison} ? LIMIT 1',(account_id,sql_owner)).fetchone():
                            raise ValueError('source inventory scope changed')
                columns = 'r.owner_id,r.job_id,r.target_workspace_id,r.source_account_id,r.source_workspace_id,r.source_ref_json,r.source_hash,r.policy_fingerprint,r.source_key,r.coverage_json,r.candidate_id,r.candidate_attempt_id,r.candidate_lease_epoch,j.kind,j.state AS job_state,j.input_hash,j.current_attempt_id,j.attempt_count'
                filters = " AND (? IS NULL OR json_extract(r.source_ref_json,'$.chat_id')=?) AND (? IS NULL OR json_extract(r.source_ref_json,'$.message_id')=?) AND (? IS NULL OR r.job_id>?)"
                args = (account_id,sql_owner,chat_id,chat_id,message_id,message_id,cursor,cursor,limit+1)
                left = db.execute(f"SELECT {columns} FROM fm_v2_s05_job_refs r INDEXED BY s04_s05_source_account LEFT JOIN fm_v2_jobs j USING(owner_id,job_id) WHERE r.source_account_id=? AND r.owner_id=? AND json_valid(r.source_ref_json){filters} ORDER BY r.job_id LIMIT ?",args).fetchall()
                right = db.execute(f"SELECT {columns} FROM fm_v2_s05_job_refs r INDEXED BY s04_s05_ref_account LEFT JOIN fm_v2_jobs j USING(owner_id,job_id) WHERE json_extract(r.source_ref_json,'$.account_id')=? AND r.owner_id=? AND json_valid(r.source_ref_json) AND r.source_account_id!=?{filters} ORDER BY r.job_id LIMIT ?",(account_id,sql_owner,account_id,chat_id,chat_id,message_id,message_id,cursor,cursor,limit+1)).fetchall()
                rows = sorted([*left,*right],key=lambda row:row['job_id'])[:limit+1]
                more = len(rows) > limit
                rows = rows[:limit]
                result = []
                for row in rows:
                    raw = json.loads(row['source_ref_json'])
                    ref = SourceRef(**raw)
                    if (json.dumps(asdict(ref),sort_keys=True,separators=(',',':')) != row['source_ref_json']
                        or _provider_source_ref_error(self, ref)
                        or ref.account_id != account_id or not _id(ref.chat_id) or not _id(ref.message_id)
                        or (chat_id is not None and ref.chat_id != chat_id)
                        or (message_id is not None and ref.message_id != message_id)
                        or row['source_account_id'] != account_id or row['kind'] not in ('s05_host_user_capture','completed_turn_review','assistant_interpretation_review')
                        or ref.workspace_id != row['source_workspace_id'] or not _hash(ref.content_hash) or ref.content_hash != row['source_hash']
                        or not _hash(row['policy_fingerprint']) or row['owner_id'] != sql_owner
                        or row['target_workspace_id'] != target.memory_workspace):
                        raise ValueError('source inventory changed')
                    if row['kind']=='assistant_interpretation_review':
                        marker=db.execute("SELECT value FROM fm_meta WHERE key='openclank.assistant_interpretation.v1'").fetchone()
                        coverage=json.loads(row['coverage_json'])
                        if (marker is None or marker[0]!='1' or type(coverage) is not dict or set(coverage)!={'version','kind','assistant','pair_sha256'}
                            or type(coverage.get('version')) is not int or coverage['version']!=1 or coverage['kind']!='assistant_interpretation_review'
                            or type(coverage['assistant']) is not dict or set(coverage['assistant'])!={'message_id','part_id','revision','content_hash'}):
                            raise ValueError('paired source coverage unavailable')
                        part=coverage['assistant']
                        pair=AssistantInterpretationRef(1,ref,part['message_id'],part['part_id'],part['revision'],part['content_hash'],coverage['pair_sha256'])
                        key=hashlib.sha256(_exact_json(['assistant_interpretation_review',asdict(ref),coverage]).encode('utf-8')).hexdigest()
                        if _assistant_ref_error(pair) or row['source_key']!=key or row['coverage_json']!=_exact_json(coverage):
                            raise ValueError('paired source binding changed')
                        if not _hash(row['input_hash']):raise ValueError('paired source input unavailable')
                        if (row['job_state']=='queued' and row['current_attempt_id'] is None and row['attempt_count']==0
                            and row['candidate_id'] is None and row['candidate_attempt_id'] is None and row['candidate_lease_epoch'] is None):
                            residue=db.execute("SELECT EXISTS(SELECT 1 FROM fm_v2_attempts WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_s05_reservations WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_job_events WHERE owner_id=?1 AND job_id=?2) OR EXISTS(SELECT 1 FROM fm_v2_job_manifests WHERE owner_id=?1 AND job_id=?2)",(row['owner_id'],row['job_id'])).fetchone()[0]
                            if residue:raise ValueError('paired source pending changed')
                            transition_class='pending'
                        elif (row['job_state']=='cancelled' and row['current_attempt_id'] is None and row['attempt_count']==0
                              and row['candidate_id'] is None and row['candidate_attempt_id'] is None and row['candidate_lease_epoch'] is None):
                            if _erased_token is not _HOST_ERASED_INVENTORY: raise ValueError('paired erased source requires verified inventory')
                            projection=_HostPairedSourceProjection(row['owner_id'],row['target_workspace_id'],row['job_id'],pair,row['policy_fingerprint'],None,row['input_hash'])
                            result.append(_HostPairedErasedCandidate(projection));continue
                        elif (row['job_state'] in ('succeeded','failed_terminal','cancelled') and type(row['current_attempt_id']) is str and row['current_attempt_id']
                              and type(row['attempt_count']) is int and row['attempt_count']>0 and row['candidate_id'] is None
                              and row['candidate_attempt_id'] is None and row['candidate_lease_epoch'] is None):
                            terminal=db.execute("SELECT a.state,a.attempt_number,a.lease_epoch,q.state AS reservation_state,q.settled_at_ms,length(CAST(j.result_json AS BLOB)) AS result_length FROM fm_v2_attempts a JOIN fm_v2_s05_reservations q ON q.owner_id=a.owner_id AND q.job_id=a.job_id AND q.attempt_id=a.attempt_id AND q.lease_epoch=a.lease_epoch JOIN fm_v2_jobs j ON j.owner_id=a.owner_id AND j.job_id=a.job_id WHERE a.owner_id=? AND a.job_id=? AND a.attempt_id=? AND j.current_attempt_id=a.attempt_id",(row['owner_id'],row['job_id'],row['current_attempt_id'])).fetchone()
                            if (terminal is None or terminal['state']!=row['job_state'] or terminal['attempt_number']!=row['attempt_count']
                                or type(terminal['lease_epoch']) is not int or terminal['lease_epoch']<=0 or terminal['reservation_state']!='settled'
                                or terminal['settled_at_ms'] is None or type(terminal['result_length']) is not int or not 0<=terminal['result_length']<=8192):
                                raise ValueError('paired terminal state unavailable')
                            terminal_raw=db.execute('SELECT result_json FROM fm_v2_jobs WHERE owner_id=? AND job_id=?',(row['owner_id'],row['job_id'])).fetchone()[0]
                            def unique_terminal(pairs):
                                obj={}
                                for k,v in pairs:
                                    if k in obj:raise ValueError('duplicate paired terminal key')
                                    obj[k]=v
                                return obj
                            value=json.loads(terminal_raw,object_pairs_hook=unique_terminal,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid paired terminal')))
                            disposition=value.get('paired_disposition') if type(value) is dict else None
                            reason={'succeeded':'no_finding','failed_terminal':'failed','cancelled':'cancelled'}[row['job_state']]
                            if (type(disposition) is not dict or set(disposition)!={'version','kind','reason','job_id','attempt_id','lease_epoch','source_key','pair_sha256'}
                                or type(disposition['version']) is not int or disposition['version']!=1 or disposition['kind']!='no_publication' or disposition['reason']!=reason
                                or disposition['job_id']!=row['job_id'] or disposition['attempt_id']!=row['current_attempt_id']
                                or type(disposition['lease_epoch']) is not int or disposition['lease_epoch']!=terminal['lease_epoch']
                                or disposition['source_key']!=row['source_key'] or disposition['pair_sha256']!=coverage['pair_sha256']):
                                raise ValueError('paired terminal disposition unavailable')
                            transition_class='terminal'
                        elif (type(row['candidate_id']) is str and row['candidate_id'] and type(row['candidate_attempt_id']) is str and row['candidate_attempt_id']
                              and type(row['candidate_lease_epoch']) is int and row['candidate_lease_epoch']>0
                              and db.execute('SELECT 1 FROM fm_v2_candidates WHERE owner_id=? AND candidate_id=?',(row['owner_id'],row['candidate_id'])).fetchone()):
                            transition_class='materialized'
                        else:raise ValueError('paired source state unavailable')
                        result.append(_HostPairedSourceProjection(row['owner_id'],row['target_workspace_id'],row['job_id'],pair,row['policy_fingerprint'],transition_class,row['input_hash']))
                    else:
                        result.append(_HostSourceProjection(row['owner_id'],row['target_workspace_id'],row['job_id'],ref,row['policy_fingerprint']))
        except (ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('host source inventory unavailable') from exc
        if resolve() != target:
            raise MemoryRequestRejectedError('host source lifecycle changed')
        if phase_capability is not None:phase_capability.assert_prefence_current(provider=self,phase=claimed_phase,account_id=account_id,sql_owner=sql_owner)
        return (tuple(result), ('j:'+rows[-1]['job_id'] if more else ('o:' if feedback else None)))

    async def _host_append_found(self, request):
        """Host-only typed observation; caller holds fresh source/lifecycle guard."""
        from src.memory_provider import FoundObservation, RecallInvocation, MemoryRevisionRef
        from src.memery_external_sources import SourceRef, _source_ref_error
        from src.openclank.memory_job_admission import _conn
        import re
        if (type(request) is not FoundObservation or type(request.version) is not int or request.version != 1
            or type(request.source) is not SourceRef or _provider_source_ref_error(self, request.source)
            or type(request.invocation) is not RecallInvocation or type(request.memory) is not MemoryRevisionRef):
            raise MemoryRequestRejectedError('found observation unavailable')
        inv, ref = request.invocation, request.memory
        if (type(request.owner) is not str or not request.owner or self._scope(request.owner).owner != request.owner
            or type(request.workspace_id) is not str or request.workspace_id != self._workspace_id
            or ref.owner != request.owner or ref.workspace_id != request.workspace_id or ref.project_id is not None
            or type(ref.version) is not int or ref.version != 1 or ref.provider != 'fm-v2'
            or type(ref.revision) is not int or ref.revision < 1 or type(ref.value_sha256) is not str
            or re.fullmatch(r'[0-9a-f]{64}',ref.value_sha256) is None or type(ref.block_id) is not str
            or not ref.block_id or ref.block_id != ref.block_id.strip() or len(ref.block_id)>512
            or inv.actor_id != 'main' or inv.root_operation_id != request.source.message_id
            or type(inv.execution_revision) is not int or inv.execution_revision < 0
            or any(type(value) is not str or not value or value != value.strip() or len(value)>512 for value in
                   (inv.session_id,inv.root_operation_id,inv.actor_id,inv.inner_turn_id,inv.recall_attempt_id))):
            raise MemoryRequestRejectedError('found observation identity unavailable')
        from src.openclank.memory_context_admission import found_features_valid, found_request_body
        if request.features is not None and (not found_features_valid(request.features) or not self._host_found_features_ready()):
            raise MemoryRequestRejectedError('found observation identity unavailable')
        try:
            with _conn(self._local_db_path()) as db:
                if not self._s06_source_capability(db): raise ValueError('observation capability unavailable')
        except (ValueError,TypeError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('observation capability unavailable') from exc
        material = json.dumps([request.owner,inv.recall_attempt_id,ref.block_id,ref.revision],ensure_ascii=False,separators=(',',':')).encode()
        expected = 'cs_s06found_' + hashlib.sha256(material).hexdigest()
        result = await self._host_candidate_request(request.owner,request.workspace_id,{'phase':'found_observation','request':found_request_body(request)},require_live=True)
        if type(result) is not dict or set(result) != {'version','event_id'} or type(result['version']) is not int or result['version'] != 1 or result['event_id'] != expected:
            raise MemoryRequestRejectedError('found observation receipt unavailable')
        return result

    async def _host_append_admitted(self, request: _HostAdmittedObservation):
        """Append only host-bound, exact acknowledged recall evidence."""
        import re
        from src.openclank.memory_job_admission import _conn
        from src.memery_external_sources import SourceRef, _source_ref_error
        if type(request) is not _HostAdmittedObservation or type(request.origin) is not _HostObservationProjection:
            raise MemoryRequestRejectedError('admitted observation unavailable')
        origin = request.origin
        if (type(origin.source) is not SourceRef or _provider_source_ref_error(self, origin.source)
            or type(origin.owner) is not str or not origin.owner
            or type(origin.workspace_id) is not str or origin.workspace_id != self._workspace_id
            or self._scope(origin.owner).owner != origin.owner
            or type(request.found_event_id) is not str or not re.fullmatch(r'cs_s06found_[0-9a-f]{64}', request.found_event_id)
            or any(type(v) is not str or not v or v != v.strip() or len(v) > 512
                   for v in (request.tool_call_id, request.attestation_id, request.dispatch_id))
            or any(type(v) is not str or re.fullmatch(r'[0-9a-f]{64}', v) is None
                   for v in (request.result_sha256, request.body_sha256))
            or not self._guarded_preparation or self._guarded_preparation.capabilities.get('admitted') is not True):
            raise MemoryRequestRejectedError('admitted observation identity unavailable')
        try:
            with _conn(self._local_db_path()) as db:
                if (not self._s06_source_capability(db) or not self._s06_admitted_capability(db)
                    or not self._s06_erase_capability(db)):
                    raise ValueError('admitted capability unavailable')
        except (ValueError, TypeError, sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('admitted capability unavailable') from exc
        authority = {key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
        payload = {'version':1, 'owner':origin.owner, 'workspace_id':origin.workspace_id,
            'found_event_id':request.found_event_id, 'authority':authority, 'tool_call_id':request.tool_call_id,
            'attestation_id':request.attestation_id, 'result_sha256':request.result_sha256,
            'dispatch_id':request.dispatch_id, 'body_sha256':request.body_sha256}
        material=json.dumps([origin.owner,request.found_event_id],ensure_ascii=False,separators=(',',':')).encode()
        expected='cs_s06admitted_'+hashlib.sha256(material).hexdigest()
        result=await self._host_candidate_request(origin.owner,origin.workspace_id,
            {'phase':'admitted_observation','request':payload},require_live=True)
        if type(result) is not dict or set(result)!={'version','event_id'} or result['version']!=1 or result['event_id']!=expected:
            raise MemoryRequestRejectedError('admitted observation receipt unavailable')
        return expected

    async def _host_append_context_admitted(self, request: _HostContextAdmittedObservation):
        """Append only host-bound, exact acknowledged recall evidence."""
        import re
        from src.openclank.memory_job_admission import _conn
        from src.memery_external_sources import SourceRef, _source_ref_error
        if type(request) is not _HostContextAdmittedObservation or type(request.origin) is not _HostObservationProjection:
            raise MemoryRequestRejectedError('admitted observation unavailable')
        origin = request.origin
        if (type(origin.source) is not SourceRef or _provider_source_ref_error(self, origin.source)
            or type(origin.owner) is not str or not origin.owner
            or type(origin.workspace_id) is not str or origin.workspace_id != self._workspace_id
            or self._scope(origin.owner).owner != origin.owner
            or type(request.found_event_id) is not str or not re.fullmatch(r'cs_s06found_[0-9a-f]{64}', request.found_event_id)
            or any(type(v) is not str or not v or v != v.strip() or len(v) > 512
                   for v in (request.context_part_id, request.attestation_id, request.dispatch_id))
            or any(type(v) is not str or re.fullmatch(r'[0-9a-f]{64}', v) is None
                   for v in (request.text_sha256, request.body_sha256))
            or not self._guarded_preparation or self._guarded_preparation.capabilities.get('admitted') is not True):
            raise MemoryRequestRejectedError('admitted observation identity unavailable')
        try:
            with _conn(self._local_db_path()) as db:
                if (not self._s06_source_capability(db) or not self._s06_admitted_capability(db)
                    or not self._s06_erase_capability(db) or not self._s06_context_admitted_capability(db)):
                    raise ValueError('admitted capability unavailable')
        except (ValueError, TypeError, sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('admitted capability unavailable') from exc
        authority = {key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
        payload = {'version':1, 'owner':origin.owner, 'workspace_id':origin.workspace_id,
            'found_event_id':request.found_event_id, 'authority':authority, 'context_part_id':request.context_part_id,
            'attestation_id':request.attestation_id, 'text_sha256':request.text_sha256,
            'dispatch_id':request.dispatch_id, 'body_sha256':request.body_sha256}
        material=json.dumps([origin.owner,request.found_event_id],ensure_ascii=False,separators=(',',':')).encode()
        expected='cs_s06context_'+hashlib.sha256(material).hexdigest()
        result=await self._host_candidate_request(origin.owner,origin.workspace_id,
            {'phase':'context_admitted_observation','request':payload},require_live=True)
        if type(result) is not dict or set(result)!={'version','event_id'} or result['version']!=1 or result['event_id']!=expected:
            raise MemoryRequestRejectedError('admitted observation receipt unavailable')
        return expected

    async def _host_append_external_history(self, owner: str, claim: _ExternalHistoryClaim) -> str:
        """Host-only inert claim receipt; caller proves owner approval and current scope."""
        _validate_external_history_claim(claim, self._workspace_id)
        expected = 'cs_s06claim_' + hashlib.sha256(json.dumps([claim.workspace_id,claim.import_id,claim.page],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        body=asdict(claim)
        for item in body['items']:
            if item['polarity'] is None: item.pop('polarity')
        result = await self._host_candidate_request(owner,claim.workspace_id,{'phase':'external_history_claim','claim':body},require_live=True)
        if type(result) is not dict or result != {'version':1,'receipt_id':expected}:
            raise MemoryRequestRejectedError('external history receipt unavailable')
        return expected

    async def _host_external_history_page(self, owner: str, workspace: str, *, after=None, limit=128):
        if workspace != self._workspace_id or type(limit) is not int or not 1 <= limit <= 128:
            raise MemoryRequestRejectedError('external history unavailable')
        result = await self._host_candidate_request(owner,workspace,{'phase':'external_history_page','after':after,'limit':limit},require_live=True)
        if (type(result) is not dict or set(result) != {'claims','next_after'} or type(result['claims']) is not list
                or len(result['claims']) > limit or (result['next_after'] is not None and type(result['next_after']) is not str)):
            raise MemoryRequestRejectedError('external history page unavailable')
        return result

    async def _host_policy_publication_status(self, *, owner, workspace_id):
        """Read the current private writer capability; unknown versions refuse."""
        result=await self._host_candidate_request(owner,workspace_id,{'phase':'policy_publication_status'},require_live=True)
        if (type(result) is not dict or set(result)!={'version','policy_writer','s08','maintenance','review'}
                or type(result['version']) is not int or result['version']!=1
                or any(type(result[key]) is not bool for key in ('policy_writer','s08','maintenance','review'))
                or (result['review'] and not result['s08'])
                or (not result['policy_writer'] and (result['s08'] or result['maintenance']))):
            raise MemoryRequestRejectedError('policy publication capability unavailable')
        return result

    def _s08_session_pairs(self, session):
        """Ordered (item, origin) pairs of the session's sources: memory consolidation pairs, or the included journal rows."""
        if session.output_kind=='journal_synthesis': return session.journal.pairs()
        from src.memery_consolidation import bounded_codelivery_pairs, bounded_importance_target_pairs
        if session.output_kind=='importance_target': return bounded_importance_target_pairs(session.selection,session.origins)
        return bounded_codelivery_pairs(session.selection,session.origins)

    def _host_s08_new_policy_session(self, lifecycle, selection, origins, policy_projection, assignment, router, output_kind, journal=None, expected_policy_refs=None):
        """Retain one private session before any possibly-issued phase."""
        from src.memery_consolidation import ConsolidationSelection, bounded_codelivery_pairs
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
        from src.openclank.memory_job_admission import AssignmentAuthority
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        from src.openclank.operation_router import ManagedOperationRouter
        from src.openclank.fm_journal_delta import JournalSelection
        journal_run=output_kind=='journal_synthesis' and type(journal) is JournalSelection and selection is None and origins==()
        if (type(lifecycle) is not CompletedTurnLifecycleOwner or lifecycle.provider is not self
            or (not journal_run and (type(selection) is not ConsolidationSelection or type(origins) is not tuple))
            or type(assignment) is not AssignmentAuthority or assignment.owner!=lifecycle.owner
            or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation
            or type(router) is not ManagedOperationRouter or (output_kind not in ('summary','connection','importance_target') and not journal_run)
            or not self._host_s08_policy_ready(output_kind) or getattr(self,'_s08_policy_session',None) is not None):
            raise MemoryRequestRejectedError('S08 policy session unavailable')
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection:
            raise MemoryRequestRejectedError('S08 policy session unavailable')
        if output_kind=='importance_target':
            from src.memery_consolidation import bounded_importance_target_pairs
            pairs=bounded_importance_target_pairs(selection,origins)
        else:
            pairs=journal.pairs() if journal_run else bounded_codelivery_pairs(selection,origins)
        if not pairs or any(item.source.account_id!=lifecycle.account_id for item,_ in pairs):
            raise MemoryRequestRejectedError('S08 policy session unavailable')
        session=_S08PolicySession(_S08_POLICY_SESSION_SEAL,self,lifecycle,selection,origins,policy_projection,assignment,router,output_kind)
        if expected_policy_refs is not None and (not journal_run or type(expected_policy_refs) is not dict):
            raise MemoryRequestRejectedError('S08 expected policy unavailable')
        session.expected_policy_refs=copy.deepcopy(expected_policy_refs)
        session.journal=journal if journal_run else None
        self._s08_policy_session=session
        return session

    async def _host_s08_prepare_policy(self, session, *, guards):
        """Evaluate current policy and retain exact Native prepare/reserve originals."""
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.memery_consolidation import bounded_codelivery_pairs, resolve_s08_native_evaluation, resolve_importance_target_evaluation, model_operation_input, s08_queue_limits
        from src.openclank.memory_job_admission import _completed_turn_limits
        from src.openclank.memory_policy_projection import policy_coordinator
        from src.openclank.operation_router import ManagedOperationRequest
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session
            or session.state!='new' or type(guards) is not dict or not self._host_s08_policy_ready(session.output_kind)):
            raise MemoryRequestRejectedError('S08 policy session unavailable')
        lifecycle=session.lifecycle; selection=session.selection; assignment=session.assignment
        binding=lifecycle.target_authority.resolve(lifecycle.account_id)
        coordinator=policy_coordinator()
        if binding!=lifecycle.binding or coordinator is None or coordinator.projection is not session.projection:
            raise MemoryRequestRejectedError('S08 policy authority unavailable')
        base,limits=_completed_turn_limits(lifecycle.target_authority,assignment)
        refs=coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,binding.memory_workspace)
        if session.expected_policy_refs is not None and refs!=session.expected_policy_refs:
            raise MemoryRequestRejectedError('S08 expected policy changed')
        keys=('memory_policy_importance_enabled','memory_policy_connections_enabled','memory_policy_consolidation_enabled')
        with PREFERENCE_LOCK:
            prefs=_load_for_user(lifecycle.owner)
            if any(key in prefs and type(prefs[key]) is not bool for key in keys): raise MemoryRequestRejectedError('S08 policy unavailable')
            flags=tuple(prefs.get(key) is True for key in keys)
            configured=copy.deepcopy(prefs.get('memory_policy_connections_predicate'))
        if session.output_kind=='summary' and not (flags[0] or flags[2]):
            self._s08_policy_session=None; session.state='retired'; return {'state':'legacy_summary'}
        if session.output_kind=='connection' and not flags[1]:
            self._s08_policy_session=None; session.state='retired'; return {'state':'deferred'}
        if session.output_kind=='importance_target' and not flags[0]:
            self._s08_policy_session=None; session.state='retired'; return {'state':'deferred'}
        journal=session.journal if session.output_kind=='journal_synthesis' else None
        if journal is not None and not flags[2]:   # consolidation preference default Off: nothing is prepared
            self._s08_policy_session=None; session.state='retired'; return {'state':'deferred'}
        work_kind='synthesis' if journal is not None else 'interpretation'
        pairs=self._s08_session_pairs(session); admissions=[]; assignments=[]; route=None
        for item,origin in pairs:
            if journal is None and (type(origin) is not _HostObservationProjection or origin.source!=item.source or origin.owner!=binding.memory_owner
                or origin.workspace_id!=binding.memory_workspace or item.source.account_id!=lifecycle.account_id):
                raise MemoryRequestRejectedError('S08 source unavailable')
            if journal is not None and item.source.account_id!=lifecycle.account_id: raise MemoryRequestRejectedError('S08 source unavailable')
            guard=guards.get(item.source.chat_id)
            if guard is None: raise MemoryRequestRejectedError('S08 source unavailable')
            lifecycle.for_source(source=item.source,guard=guard)
            if journal is None:
                authority={key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
                admissions.append({'event_id':item.event_id,'source':asdict(item.source),'authority':authority})
        admissions.sort(key=lambda item:item['event_id'])
        if journal is not None:
            selected,journal_digest=journal.selected(owner=binding.memory_owner,workspace_id=binding.memory_workspace,account_id=lifecycle.account_id,
                target_fingerprint=binding.fingerprint,policy_fingerprint=base['fingerprint'],limits=limits)
        else:
            candidates=sorted((asdict(item) for item in selection.memories),key=lambda item:item['block_id'])
            material={'account':lifecycle.account_id,'target':binding.fingerprint,'policy':base['fingerprint'],'kind':session.output_kind,
                      'candidates':candidates,'admissions':admissions}
            key='s08_'+hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
            selected={'version':1,'owner':binding.memory_owner,'workspace_id':binding.memory_workspace,'project_id':None,
                'source_account_id':lifecycle.account_id,'idempotency_key':key,'target_fingerprint':binding.fingerprint,
                'policy_fingerprint':base['fingerprint'],**s08_queue_limits(limits),
                'candidates':candidates,'admissions':admissions}
        def current():
            try:
                lifecycle.profile.assert_current()
                if (self._s08_policy_session is not session or self._s08_policy_witness is not session.profile_witness or not self._host_s08_policy_ready(session.output_kind)
                    or lifecycle.target_authority.resolve(lifecycle.account_id)!=binding
                    or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)
                    or coordinator.projection is not session.projection
                    or coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,binding.memory_workspace)!=refs
                    or _completed_turn_limits(lifecycle.target_authority,assignment)!=(base,limits)): return False
                with PREFERENCE_LOCK:
                    now=_load_for_user(lifecycle.owner)
                    if tuple(now.get(k) is True for k in keys)!=flags or now.get('memory_policy_connections_predicate')!=configured: return False
                for index,(item,_) in enumerate(pairs):
                    lifecycle.for_source(source=item.source,guard=guards[item.source.chat_id])
                    if assignments and assignment.resolve(work_kind,journal.anchor if journal is not None else item.source)!=assignments[index]: return False
                return True
            except Exception: return False
        if not current(): raise MemoryRequestRejectedError('S08 authority changed')
        target_facts=None
        if session.output_kind=='importance_target':
            ref=selection.memories[0]
            target_facts=await self._host_s02_importance_read(owner=binding.memory_owner,workspace_id=binding.memory_workspace,
                account_id=lifecycle.account_id,phase='resolve_target',block_id=ref.block_id,expected_revision=ref.revision,
                policy=refs,current=current,deadline=float(asyncio.get_running_loop().time()+1.5))
            selected['importance_target']=copy.deepcopy(target_facts['target'])
        if journal is not None:
            # The journal has no memory-block snapshot or evaluation axis: model mode only (rules mode makes no semantic claim).
            snapshot={'commitment_sha256':journal_digest}; decision=values=None; mode='model'
        else:
            snapshot=await self._host_s08_policy_snapshots(owner=binding.memory_owner,account_id=lifecycle.account_id,
                workspace_id=binding.memory_workspace,policy_user=lifecycle.owner,selection=selected,policy_projection=session.projection,current=current)
            if session.output_kind=='importance_target':
                decision,value_json=resolve_importance_target_evaluation(selection,snapshot,target=target_facts['target'],score_context=target_facts['score_context'])
                if decision is None:
                    self._s08_policy_session=None;session.state='retired';return {'state':'deferred'}
                mode,score=decision
                current_score=target_facts['score_context']['importance']['value']
                if mode=='rules' and score==current_score:
                    self._s08_policy_session=None;session.state='retired';return {'state':'deferred'}
                values=((selection.memories[0].block_id,selection.memories[0].revision,value_json),)
            else:
                primary_axis='consolidation' if session.output_kind=='summary' else 'connections'
                decision,values=resolve_s08_native_evaluation(selection,snapshot,axis=primary_axis)
                if session.output_kind=='summary':
                    mode=decision if flags[2] else 'rules'
                else:
                    mode=decision[2] if decision and decision[0]=='resolved' else None
        if mode not in ('rules','model','hybrid'):
            self._s08_policy_session=None; session.state='retired'; return {'state':'deferred'}
        if mode!='rules':
            try:
                assignments=[assignment.resolve(work_kind,journal.anchor if journal is not None else item.source) for item,_ in pairs]
            except ValueError:
                if journal is None: raise
                self._s08_policy_session=None; session.state='retired'; return {'state':'deferred'}   # unset + unanchored: defers
            fields=('connectionID','modelRouteID','modelID','grantID','optionRouteID','catalogRevision','options')
            route=assignments[0]
            if any({key:value.get(key) for key in fields}!={key:route.get(key) for key in fields} for value in assignments[1:]):
                self._s08_policy_session=None; session.state='retired'; return {'state':'deferred'}
        predicate=None
        if session.output_kind=='connection':
            if type(configured) is not dict or set(configured)!={'predicate','version','sha256'}:
                self._s08_policy_session=None; session.state='retired'; return {'state':'deferred'}
            options=await self._host_registered_connection_predicates(owner=binding.memory_owner,account_id=lifecycle.account_id,
                workspace_id=binding.memory_workspace,policy_user=lifecycle.owner,policy_projection=session.projection,current=current)
            if configured not in options['predicates']: raise MemoryRequestRejectedError('S08 predicate unavailable')
            predicate=configured
        importance={'state':'skipped','reason':'disabled'}
        if journal is not None: importance={'state':'skipped','reason':'unset'}
        elif session.output_kind=='importance_target':
            importance=({'state':'rules','value':score,'provenance_sha256':snapshot['commitment_sha256']} if mode=='rules'
                else {'state':'managed','mode':mode,'provenance_sha256':snapshot['commitment_sha256']})
        elif flags[0]:
            imp,_=resolve_s08_native_evaluation(selection,snapshot,axis='importance')
            if imp is None: importance={'state':'skipped','reason':'unavailable'}
            elif imp[0]=='unset': importance={'state':'skipped','reason':'unset'}
            elif imp[2]=='rules': importance={'state':'rules','value':imp[1],'provenance_sha256':snapshot['commitment_sha256']}
            elif imp[2]==mode and mode in ('model','hybrid'): importance={'state':'managed','mode':mode,'provenance_sha256':snapshot['commitment_sha256']}
            else: importance={'state':'skipped','reason':'incompatible'}
        operation=None; input_bound=0
        if journal is not None:
            from src.openclank.fm_journal_delta import JournalDeltaError
            try: operation,input_bound=journal.model_input(lifecycle.source_authority.hydrate,limits.job_output_tokens)
            except JournalDeltaError:
                self._s08_policy_session=None; session.state='retired'; return {'state':'deferred'}   # changed/withdrawn source: no write
        elif mode!='rules':
            operation,input_bound=model_operation_input(selection,values,limits.job_output_tokens,output_kind=session.output_kind,predicate=predicate,
                allow_importance=False if target_facts is not None else importance['state']=='managed',target=target_facts['target'] if target_facts is not None else None)
        from src.openclank.fm_journal_delta import journal_assignment_sha256, journal_original
        assignment_sha=journal_assignment_sha256(journal,route) if journal is not None else hashlib.sha256(json.dumps([{'source':asdict(item.source),'assignment':value} for (item,_),value in zip(pairs,assignments)],sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest() if mode!='rules' else hashlib.sha256(b'no-model').hexdigest()
        original=journal_original(selected=selected,digest=journal_digest,route=route,assignment_sha256=assignment_sha,input_bound=input_bound,limits=limits,policy_refs=refs,cadence_policy=journal.cadence_policy) if journal is not None else {'version':1,'selection':selected,'mode':mode,'output_kind':session.output_kind,'connection_predicate':predicate,
            'importance':importance,'snapshots_sha256':snapshot['commitment_sha256'],'assignment_sha256':assignment_sha,
            'connection_id':route['connectionID'] if mode!='rules' else 'local:rules','model_route_id':route['modelRouteID'] if mode!='rules' else 'local:rules',
            'max_input_tokens':input_bound,'max_output_tokens':limits.job_output_tokens if mode!='rules' else 0,
            'daily_calls':limits.daily_calls if mode!='rules' else 0,'daily_tokens':limits.daily_tokens if mode!='rules' else 0,'policy_projection':refs}
        if target_facts is not None:
            original['importance_target']=copy.deepcopy(target_facts['target'])
            session.importance_target=copy.deepcopy(target_facts['target'])
        if not current(): raise MemoryRequestRejectedError('S08 authority changed')
        session.axis_flags=flags; session.policy_refs=refs; session.base=base; session.assignment_snapshot=tuple(assignments); session.predicate_configured=copy.deepcopy(configured)
        session.snapshot_commitment=snapshot['commitment_sha256']
        from src.openclank.chat_source_bridge import mint_review_currentness
        from src.openclank.memory_job_admission import _completed_turn_thread
        canonical_current=current
        def validate_preflight():
            if not canonical_current(): raise MemoryRequestRejectedError('S08 authority changed')
        source_guards=tuple((item.source,guards.get(item.source.chat_id)) for item,_ in pairs)
        if any(guard is None for _,guard in source_guards): raise MemoryRequestRejectedError('S08 source unavailable')
        (phase_witness,_),cancelled=await _completed_turn_thread(lambda: mint_review_currentness(
            lifecycle_owner=lifecycle,sources=source_guards,assignment=assignment,validate=validate_preflight))
        if cancelled:
            phase_witness.close()
            raise asyncio.CancelledError
        def current():
            try:
                if self._s08_policy_session is not session: return False
                phase_witness.assert_current(); return True
            except Exception: return False
        try:
            session.original=json.loads(json.dumps(original,ensure_ascii=False,allow_nan=False))
            session.guards=guards; session.state='prepare_issued'; session.phase_original=session.original; session.ticket={}
            try:
                prepared_job=await self._host_candidate_request(binding.memory_owner,binding.memory_workspace,
                    {'phase':'s08_model_prepare','request':session.original},require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),
                    before_issue=current,write_status=session.ticket)
            except BaseException:
                # The owner creates the ticket before it can enqueue or issue any frame.
                if not session.ticket and self._s08_policy_session is session:
                    session.state='retired';self._s08_policy_session=None
                raise
            if type(prepared_job) is not dict or set(prepared_job)!={'version','job_id'} or prepared_job['version']!=1 or type(prepared_job['job_id']) is not str:
                raise MemoryRequestRejectedError('S08 prepare outcome unavailable')
            job_id=prepared_job['job_id'];session.prepared_job_id=job_id
            now_tick=lifecycle.profile.monotonic_tick()
            session.started_at=asyncio.get_running_loop().time(); session.deadline=session.started_at+limits.job_seconds
            reservation={'version':1,'owner':binding.memory_owner,'job_id':job_id,
                'worker_id':'s08_'+hashlib.sha256((lifecycle.account_id+'\0'+lifecycle.profile.coverage_generation+'\0'+lifecycle.profile.clock_generation).encode()).hexdigest(),
                'coverage_generation':lifecycle.profile.coverage_generation,'clock_generation':lifecycle.profile.clock_generation,
                'now_tick':now_tick,'quiet_seconds':limits.quiet_seconds,'job_seconds':limits.job_seconds,
                'daily_seconds':limits.daily_seconds,'target_fingerprint':binding.fingerprint,'policy_fingerprint':base['fingerprint']}
            reserve={'version':1,'original':session.original,'reservation':reservation}
            # Retain evaluated inputs before a lost reserve reply; never re-evaluate from a new selection.
            session.mode=mode; session.values=copy.deepcopy(values); session.predicate=copy.deepcopy(predicate)
            session.importance=copy.deepcopy(importance); session.managed_operation=copy.deepcopy(operation)
            session.managed_route=copy.deepcopy(route)
            session.state='reserve_issued'; session.phase_original=json.loads(json.dumps(reserve,ensure_ascii=False,allow_nan=False)); session.ticket={}
            try:
                lease=await self._host_candidate_request(binding.memory_owner,binding.memory_workspace,
                    {'phase':'s08_model_reserve','request':session.phase_original},require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),
                    before_issue=current,write_status=session.ticket)
            except BaseException:
                # A reserve without a ticket never entered the owner queue; the prepared intent remains.
                if not session.ticket and self._s08_policy_session is session: session.state='prepare_confirmed'
                raise
            if (type(lease) is not dict or set(lease)!={'attempt_id','lease_epoch','foreground_epoch','reserve_receipt_id','request_sha256'} or type(lease['attempt_id']) is not str
                or type(lease['lease_epoch']) is not int or lease['lease_epoch']<1 or type(lease['foreground_epoch']) is not int
                or type(lease['reserve_receipt_id']) is not str or re.fullmatch(r'cs_s08modelreserve_[0-9a-f]{64}',lease['reserve_receipt_id']) is None
                or type(lease['request_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',lease['request_sha256']) is None):
                raise MemoryRequestRejectedError('S08 reservation outcome unavailable')
            session.reserve_receipt_id=lease['reserve_receipt_id']; session.reserve_request_sha256=lease['request_sha256']; session.foreground_epoch=lease['foreground_epoch']
            session.attempt={'version':1,'original':session.original,'reservation_original':session.phase_original,'job_id':job_id,
                'attempt_id':lease['attempt_id'],'lease_epoch':lease['lease_epoch']}
            if not current(): raise MemoryRequestRejectedError('S08 authority changed')
            session.mode=mode; session.values=values; session.predicate=predicate; session.importance=importance
            if mode!='rules':
                route_options={'version':1,'routeID':route['modelRouteID'],'optionRouteID':route['optionRouteID'],
                    'catalogRevision':route['catalogRevision'],'model':{'providerID':route['connectionID'],'modelID':route['modelID']},'options':route['options']}
                rid=hashlib.sha256(json.dumps([binding.memory_owner,job_id,lease['attempt_id'],lease['lease_epoch']],separators=(',',':')).encode()).hexdigest()
                managed=ManagedOperationRequest(owner=lifecycle.owner,operation='chat.complete',input=operation,purpose='memory',
                    model_route_id=route['modelRouteID'],grant_id=route['grantID'],root_operation_id='s08-'+rid,idempotency_key=rid,
                    options={'modelRequestOptions':route_options})
                session.prepared=session.router.prepare(managed)
                session.background=_S08BackgroundReservation(_S08_POLICY_SESSION_SEAL,session,session.prepared)
                session.handle=session.router.new_prepared_owned(session.prepared,background_reservation=session.background)
            session.guards=None; session.state='prepared'
            return {'state':'prepared'}

        finally:
            phase_witness.close()

    def _assert_s08_guarded_session(self, session, guards):
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.memery_consolidation import bounded_codelivery_pairs
        from src.openclank.memory_job_admission import _completed_turn_limits
        from src.openclank.memory_policy_projection import policy_coordinator
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session
            or self._s08_policy_witness is not session.profile_witness
            or type(guards) is not dict or not self._host_s08_policy_ready(session.output_kind)):
            raise MemoryRequestRejectedError('S08 session authority unavailable')
        lifecycle=session.lifecycle; lifecycle.profile.assert_current(); target=lifecycle.target_authority.resolve(lifecycle.account_id)
        coordinator=policy_coordinator()
        if (target!=lifecycle.binding or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)
            or coordinator is None or coordinator.projection is not session.projection
            or coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,target.memory_workspace)!=session.policy_refs
            or _completed_turn_limits(lifecycle.target_authority,session.assignment)[0]!=session.base):
            raise MemoryRequestRejectedError('S08 session authority changed')
        with PREFERENCE_LOCK:
            prefs=_load_for_user(lifecycle.owner)
            keys=('memory_policy_importance_enabled','memory_policy_connections_enabled','memory_policy_consolidation_enabled')
            if (tuple(prefs.get(key) is True for key in keys)!=session.axis_flags
                or prefs.get('memory_policy_connections_predicate')!=session.predicate_configured):
                raise MemoryRequestRejectedError('S08 policy choice changed')
        pairs=self._s08_session_pairs(session)
        if session.mode!='rules' and (type(session.assignment_snapshot) is not tuple or len(session.assignment_snapshot)!=len(pairs)):
            raise MemoryRequestRejectedError('S08 assignment changed')
        for index,(item,_) in enumerate(pairs):
            guard=guards.get(item.source.chat_id)
            if guard is None: raise MemoryRequestRejectedError('S08 source unavailable')
            lifecycle.for_source(source=item.source,guard=guard)
            kind_,src_=('synthesis',session.journal.anchor) if session.journal is not None else ('interpretation',item.source)
            if session.mode!='rules' and session.assignment.resolve(kind_,src_)!=session.assignment_snapshot[index]:
                raise MemoryRequestRejectedError('S08 assignment changed')
        if session.journal is not None and not session.journal.source_unchanged(lifecycle.source_authority.hydrate):
            raise MemoryRequestRejectedError('S08 source changed')   # re-verified immediately before use; no write
        lifecycle.profile.assert_current()

    async def _host_s08_ready_policy(self, session):
        """Wait for the original inert handle outside all source guards."""
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session or session.state!='prepared'):
            raise MemoryRequestRejectedError('S08 ready unavailable')
        if session.mode=='rules': session.state='ready'; return {'state':'ready'}
        if session.handle is None: raise MemoryRequestRejectedError('S08 handle unavailable')
        session.state='ready_wait'
        result=await session.handle.ready(deadline=session.deadline)
        if type(result) is not dict or set(result)!={'state'} or result['state'] not in ('ready','suppressed','unknown'):
            raise MemoryRequestRejectedError('S08 ready outcome unavailable')
        session.state='ready' if result['state']=='ready' else 'ready_'+result['state']
        return result

    async def _host_s08_dispatch_policy(self, session, *, guards):
        """Validate in a drained worker, dispatch, then arm under the same held guards."""
        from src.memery_consolidation import bounded_codelivery_pairs
        from src.openclank.chat_source_bridge import mint_review_currentness
        from src.openclank.memory_job_admission import _completed_turn_thread
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session
            or session.state!='ready' or type(session.attempt) is not dict or type(guards) is not dict):
            raise MemoryRequestRejectedError('S08 dispatch unavailable')
        lifecycle=session.lifecycle; target=lifecycle.binding
        pairs=self._s08_session_pairs(session)
        sources=tuple((item.source,guards.get(item.source.chat_id)) for item,_ in pairs)
        if any(guard is None for _,guard in sources): raise MemoryRequestRejectedError('S08 source unavailable')
        (witness,_),cancelled=await _completed_turn_thread(lambda: mint_review_currentness(
            lifecycle_owner=lifecycle,sources=sources,assignment=session.assignment,
            validate=lambda: self._assert_s08_guarded_session(session,guards)))
        if cancelled:
            witness.close()
            raise asyncio.CancelledError
        session.guards=guards;session.currentness=witness
        def current():
            try:
                if self._s08_policy_session is not session or session.currentness is not witness: return False
                witness.assert_current(); return True
            except Exception: return False
        try:
            session.state='dispatch_issued'; session.phase_original=copy.deepcopy(session.attempt); session.ticket={}
            try:
                result=await self._host_candidate_request(target.memory_owner,target.memory_workspace,
                    {'phase':'s08_model_dispatch','request':session.phase_original},require_live=True,
                    write_deadline=float(min(session.deadline,asyncio.get_running_loop().time()+30.0)),before_issue=current,write_status=session.ticket)
            except BaseException:
                if not session.ticket and self._s08_policy_session is session: session.state='ready'
                raise
            expected={'version':1,'state':'dispatched','job_id':session.attempt['job_id'],
                      'attempt_id':session.attempt['attempt_id'],'lease_epoch':session.attempt['lease_epoch']}
            if result!=expected or type(result.get('version')) is not int: raise MemoryRequestRejectedError('S08 dispatch outcome unavailable')
            if not current(): raise MemoryRequestRejectedError('S08 dispatch authority changed')
            session.state='dispatched'
            if session.mode=='rules': return {'state':'issued'}
            permit=_S08ManagedSendPermit(_S08_POLICY_SESSION_SEAL,session,session.prepared)
            session.permit=permit
            def before_issue():
                permit.assert_current(session.prepared)
                return None
            result=await session.handle.arm(before_issue=before_issue,deadline=session.deadline)
            if type(result) is not dict or set(result)!={'state'} or result['state'] not in ('issued','suppressed','unknown'):
                raise MemoryRequestRejectedError('S08 frame outcome unavailable')
            session.state='model_'+result['state']
            return result
        finally:
            session.guards=None;session.currentness=None;witness.close()

    async def _host_s08_run_policy(self, session):
        """Wait for the one already-issued handle; never create a second request."""
        from src.memery_consolidation import validate_model_proposal, validate_connection_proposal
        from src.openclank.operation_router import ManagedOperationResult
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session
            or session.guards is not None):
            raise MemoryRequestRejectedError('S08 original unavailable')
        if session.mode=='rules' and session.state=='dispatched':
            session.proposal={'version':1,'kind':'rules'}; session.usage=None; session.state='terminal'
            return {'state':'terminal'}
        if session.state!='model_issued' or session.handle is None:
            raise MemoryRequestRejectedError('S08 original requires status')
        session.state='model_waiting'
        try:
            remaining=session.deadline-asyncio.get_running_loop().time()
            if remaining<=0: session.state='model_unknown'; return {'state':'unknown'}
            result=await asyncio.wait_for(session.handle.wait(),timeout=remaining)
        except asyncio.TimeoutError:
            session.state='model_unknown'; return {'state':'unknown'}
        except asyncio.CancelledError:
            session.state='model_unknown'; raise
        except Exception:
            session.state='model_unknown'; return {'state':'unknown'}
        if type(result) is not ManagedOperationResult or result.commit_reason=='outcome_unknown':
            session.state='model_unknown'; return {'state':'unknown'}
        tokens=(result.usage.get('inputTokens'),result.usage.get('outputTokens'))
        elapsed=max(0,int(asyncio.get_running_loop().time()-session.started_at+0.999))
        session.usage=({'elapsed_seconds':elapsed,'input_tokens':tokens[0],'output_tokens':tokens[1]}
            if elapsed<=10000000 and all(type(v) is int and 0<=v<=10000000 for v in tokens) else None)
        if result.state in ('failed','cancelled'):
            session.terminal_disposition=result.state; session.state='terminal'; return {'state':'terminal'}
        if result.state!='complete':
            session.state='model_unknown'; return {'state':'unknown'}
        if not isinstance(result.output, Mapping):
            session.terminal_disposition='failed'; session.terminal_reason='invalid_proposal'; session.state='terminal'
            return {'state':'terminal'}
        try:
            allow=session.importance['state']=='managed'
            if session.output_kind=='journal_synthesis':
                from src.openclank.fm_journal_delta import validate_model_proposal as validate_journal_proposal
                session.proposal=validate_journal_proposal(result.output.get('text'))
            elif session.output_kind=='summary':
                session.proposal=validate_model_proposal(result.output.get('text'),session.selection,allow_importance=allow)
            elif session.output_kind=='importance_target':
                session.proposal=validate_model_proposal(result.output.get('text'),session.selection,output_kind='importance_target',target=session.importance_target)
            else:
                session.proposal=validate_connection_proposal(result.output.get('text'),session.selection,session.predicate,allow_importance=allow)
        except (ValueError,TypeError,UnicodeError):
            # The original model completed, but its untrusted proposal was invalid.
            session.terminal_disposition='failed'; session.terminal_reason='invalid_proposal'; session.state='terminal'
            return {'state':'terminal'}
        session.terminal_disposition=None; session.state='terminal'
        return {'state':'terminal'}

    def _host_s08_policy_disposition(self, session):
        """Classify only an already-owned, locally validated result."""
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session):
            raise MemoryRequestRejectedError('S08 original unavailable')
        if session.state in ('ready_suppressed','model_suppressed'):
            return 'terminal'
        if session.state!='terminal': return 'unknown'
        if getattr(session,'terminal_disposition',None) in ('failed','cancelled'): return 'terminal'
        proposal=getattr(session,'proposal',None)
        if type(proposal) is not dict: return 'unknown'
        if proposal.get('kind')=='no_proposal': return 'no_proposal'
        if session.output_kind=='importance_target' and proposal.get('purpose')=='importance_target' and proposal.get('version')=='s08.importance-target.v1': return 'proposal'
        return 'proposal' if proposal.get('kind') in ('proposal','rules','journal_proposal') else 'unknown'

    async def _host_s08_commit_policy(self, session, *, guards=None, cleanup_current=None):
        """Commit one known original result; candidate publication needs fresh source guards."""
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session
            or session.state!='terminal' or type(session.attempt) is not dict):
            raise MemoryRequestRejectedError('S08 original unavailable')
        if getattr(session,'terminal_disposition',None) is not None:
            return {'version':1,'state':'unknown'}  # Owned terminal settlement has a distinct private phase.
        proposal=getattr(session,'proposal',None)
        if (type(proposal) is not dict or (proposal.get('kind') not in ('rules','proposal','no_proposal','journal_proposal')
            and not (session.output_kind=='importance_target' and proposal.get('purpose')=='importance_target' and proposal.get('version')=='s08.importance-target.v1'))):
            raise MemoryRequestRejectedError('S08 original unavailable')
        lifecycle=session.lifecycle; target=lifecycle.binding; source_required=proposal.get('kind')!='no_proposal'
        phase_witness=None
        if source_required:
            from src.memery_consolidation import bounded_codelivery_pairs
            from src.openclank.chat_source_bridge import mint_review_currentness
            from src.openclank.memory_job_admission import _completed_turn_thread
            pairs=self._s08_session_pairs(session)
            source_guards=tuple((item.source,guards.get(item.source.chat_id)) for item,_ in pairs) if type(guards) is dict else ()
            if not source_guards or any(guard is None for _,guard in source_guards): raise MemoryRequestRejectedError('S08 source unavailable')
            (phase_witness,_),cancelled=await _completed_turn_thread(lambda: mint_review_currentness(
                lifecycle_owner=lifecycle,sources=source_guards,assignment=session.assignment,
                validate=lambda: self._assert_s08_guarded_session(session,guards)))
            if cancelled:
                phase_witness.close()
                raise asyncio.CancelledError
        def current():
            try:
                if self._s08_policy_session is not session: return False
                if not source_required:
                    return callable(cleanup_current) and cleanup_current() is True
                if not self._host_s08_policy_ready(session.output_kind): return False
                phase_witness.assert_current()
                return True
            except Exception: return False
        try:
            if not current(): raise MemoryRequestRejectedError('S08 original authority unavailable')
            request={'version':1,'attempt':copy.deepcopy(session.attempt),'proposal':copy.deepcopy(proposal),'usage':copy.deepcopy(session.usage)}
            session.state='commit_issued'; session.phase_original=copy.deepcopy(request); session.ticket={}
            try:
                result=await self._host_candidate_request(target.memory_owner,target.memory_workspace,
                    {'phase':'s08_policy_commit','request':session.phase_original},require_live=True,
                    write_deadline=float(asyncio.get_running_loop().time()+30.0),before_issue=current,write_status=session.ticket)
            except BaseException:
                if not session.ticket and self._s08_policy_session is session: session.state='terminal'
                raise
            if (type(result) is not dict or set(result)!={'version','state','candidate_id'} or type(result['version']) is not int
                or result['version']!=1 or result['state'] not in ('staged','no_proposal')
                or (result['state']=='staged' and (type(result['candidate_id']) is not str or not result['candidate_id']))
                or (result['state']=='no_proposal' and result['candidate_id'] is not None)):
                raise MemoryRequestRejectedError('S08 original outcome unavailable')
            # Keep the correlated positive receipt before checking delivery authority.
            session.result=copy.deepcopy(result);delivery_current=current()
            session.state='settled'
            if self._s08_policy_session is session: self._s08_policy_session=None
            if not delivery_current: raise MemoryRequestRejectedError('S08 original authority changed')
            return result

        finally:
            if phase_witness is not None: phase_witness.close()

    async def _host_s08_cancel_policy(self, session, *, lifecycle, current):
        """Cancel and settle only the exact original when no send is proved."""
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session
            or lifecycle.provider is not self or lifecycle.account_id!=session.lifecycle.account_id
            or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('S08 cancellation unavailable')
        if session.state=='prepare_confirmed':
            if type(session.original) is not dict or type(getattr(session,'prepared_job_id',None)) is not str:
                raise MemoryRequestRejectedError('S08 prepared original unavailable')
            target=lifecycle.target_authority.resolve(lifecycle.account_id)
            original=copy.deepcopy(session.original);session.state='prepared_cancel_issued';session.phase_original=original;session.ticket={}
            try:
                result=await self._host_candidate_request(target.memory_owner,target.memory_workspace,
                    {'phase':'s08_policy_prepared_cancel','request':original},require_live=True,
                    write_deadline=float(asyncio.get_running_loop().time()+30.0),before_issue=current,write_status=session.ticket)
            except BaseException:
                if not session.ticket and self._s08_policy_session is session: session.state='prepare_confirmed'
                raise
            if result!={'version':1,'state':'cancelled','job_id':session.prepared_job_id} or type(result.get('version')) is not int:
                raise MemoryRequestRejectedError('S08 prepared cancellation unavailable')
            session.result=copy.deepcopy(result)
            try: delivery_current=current() is True and self._s08_policy_session is session
            except Exception: delivery_current=False
            session.state='settled'
            if self._s08_policy_session is session: self._s08_policy_session=None
            if not delivery_current: raise MemoryRequestRejectedError('S08 cancellation authority changed')
            return result
        if type(session.attempt) is not dict: return {'version':1,'state':'unknown'}
        if session.state=='reserve_confirmed_unsent' and session.handle is None:
            if session.mode=='rules':
                session.terminal_disposition='cancelled'; session.usage=None; session.state='terminal'
            else:
                session.state='confirmed_unsent'
            return await self._host_s08_terminal_policy(session,lifecycle=lifecycle,current=current)
        if getattr(session,'mode',None)=='rules' and session.state in ('prepared','ready','dispatch_confirmed_unsent','dispatched','terminal') and getattr(session,'terminal_disposition',None) is None:
            session.terminal_disposition='cancelled'; session.usage=None; session.state='terminal'
            return await self._host_s08_terminal_policy(session,lifecycle=lifecycle,current=current)
        # A validated, completed managed response can be discarded without publication.
        if (session.state=='terminal' and getattr(session,'terminal_disposition',None) is None
            and getattr(session,'mode',None) in ('model','hybrid') and type(getattr(session,'proposal',None)) is dict):
            session.terminal_disposition='cancelled'; session.terminal_reason=None
            return await self._host_s08_terminal_policy(session,lifecycle=lifecycle,current=current)
        if session.state not in ('prepared','ready_wait','ready','dispatch_confirmed_unsent','ready_suppressed','model_suppressed') or session.handle is None:
            return {'version':1,'state':'unknown'}
        await session.handle.cancel()
        if await session.handle.drain(deadline=session.deadline)!='suppressed':
            return {'version':1,'state':'unknown'}
        if current() is not True: raise MemoryRequestRejectedError('S08 cancellation authority changed')
        session.state='ready_suppressed'
        return await self._host_s08_terminal_policy(session,lifecycle=lifecycle,current=current)

    async def _host_s08_terminal_policy(self, session, *, lifecycle, current):
        """Settle only this owned content-free terminal; unknown never permits another issue."""
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session
            or lifecycle.provider is not self or lifecycle.account_id!=session.lifecycle.account_id
            or type(session.attempt) is not dict or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('S08 terminal unavailable')
        disposition=None; usage=None
        if session.state=='confirmed_unsent' and session.handle is None:
            # The retained reserve returned only through status before any handle was created.
            disposition='positively_unsent'
        elif session.state in ('ready_suppressed','model_suppressed'):
            if session.handle is None: raise MemoryRequestRejectedError('S08 suppression unavailable')
            # Only the same fully drained task can prove the writer sent no frame.
            if await session.handle.drain(deadline=session.deadline)!='suppressed':
                return {'version':1,'state':'unknown'}
            disposition='positively_unsent'
        elif session.state=='terminal' and getattr(session,'terminal_disposition',None) in ('failed','cancelled'):
            reason=getattr(session,'terminal_reason',None)
            if reason is not None and (session.terminal_disposition!='failed' or reason!='invalid_proposal'):
                raise MemoryRequestRejectedError('S08 terminal reason unavailable')
            disposition=session.terminal_disposition; usage=copy.deepcopy(session.usage)
        else:
            return {'version':1,'state':'unknown'}
        if current() is not True or self._s08_policy_session is not session:
            raise MemoryRequestRejectedError('S08 terminal authority changed')
        target=lifecycle.target_authority.resolve(lifecycle.account_id)
        request={'version':1,'attempt':copy.deepcopy(session.attempt),'disposition':disposition,'usage':usage}
        if disposition=='failed' and getattr(session,'terminal_reason',None)=='invalid_proposal':
            request['reason']='invalid_proposal'
        previous_state=session.state
        session.state='terminal_issued'; session.phase_original=copy.deepcopy(request); session.ticket={}
        try:
            result=await self._host_candidate_request(target.memory_owner,target.memory_workspace,
                {'phase':'s08_policy_terminal','request':session.phase_original},require_live=True,
                write_deadline=float(asyncio.get_running_loop().time()+30.0),before_issue=current,write_status=session.ticket)
        except BaseException:
            if not session.ticket and self._s08_policy_session is session: session.state=previous_state
            raise
        if (type(result) is not dict or result!={'version':1,'state':'settled','disposition':disposition}
            or type(result.get('version')) is not int):
            raise MemoryRequestRejectedError('S08 terminal outcome unavailable')
        session.result=copy.deepcopy(result)
        try: delivery_current=current() is True and self._s08_policy_session is session
        except Exception: delivery_current=False
        session.state='settled'
        if self._s08_policy_session is session: self._s08_policy_session=None
        if not delivery_current: raise MemoryRequestRejectedError('S08 terminal authority changed')
        return result

    def _document_vector_selection_context(self, authority):
        """Capture one already-fenced account preference, never model switches."""
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.openclank.memory_policy_projection import policy_coordinator,preference_inputs,exact_json
        from src.openclank.document_vector_settings import selected
        coordinator=policy_coordinator();owner=authority.owner;account=authority.account
        if coordinator is None:raise MemoryRequestRejectedError('document vector policy unavailable')
        resolved,target=coordinator.resolve(owner)
        if resolved!=account or target!=self._scope(owner).workspace_id:raise MemoryRequestRejectedError('document vector policy unavailable')
        refs=coordinator.checked_refs(owner,account,target)
        with PREFERENCE_LOCK:
            prefs=_load_for_user(owner);selection=selected(prefs);inputs=preference_inputs(prefs)
        candidates=[exact_json({'preferences':inputs,'can_manage_memory':value}) for value in (False,True)]
        matching=[raw for raw in candidates if hashlib.sha256(raw.encode('utf-8')).hexdigest()==refs['account']['fingerprint']]
        if len(matching)!=1:raise MemoryRequestRejectedError('document vector policy unavailable')
        policy={'workspace_id':target,'policy_projection':refs,'policy_input_json':matching[0]}
        def recheck():
            try:
                if policy_coordinator() is not coordinator or coordinator.resolve(owner)!=(account,target):return False
                if coordinator.checked_refs(owner,account,target)!=refs:return False
                with PREFERENCE_LOCK:current=_load_for_user(owner);return selected(current)==selection and preference_inputs(current)==inputs
            except Exception:return False
        if recheck() is not True:raise MemoryRequestRejectedError('document vector policy changed')
        return selection,policy,recheck

    async def _host_document_vector_profile_build(self, authority, *, deadline):
        """Explicitly build one selected profile; status never starts another."""
        from routes.personal_routes import _PersonalDocumentAuthority
        from src.openclank.document_vector_settings import choice
        from src.openclank.mimo_supervisor import _queued_owned_thread
        if (type(authority) is not _PersonalDocumentAuthority or authority.snapshot is not None or authority.current_account() is not True
                or type(deadline) is not float or not math.isfinite(deadline) or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('document vector profile unavailable')
        owner=authority.owner;workspace=self._workspace_id;prepared=self._guarded_preparation
        transport=self._owned_transport;generation=self._owned_generation;process=transport.process;task=self._owner_task;loop=asyncio.get_running_loop()
        selection,policy,policy_current=self._document_vector_selection_context(authority)
        selected_choice=choice(selection)
        if selected_choice is None:raise MemoryRequestRejectedError('select a vector profile first')
        if not self._native_memory_ops_ready() and not await self._host_negotiate_native_memory_ops(owner=owner,workspace_id=workspace,deadline=deadline):
            raise MemoryRequestRejectedError('document vector profile unavailable')
        if NATIVE_VECTOR_PROFILE_OPERATION not in self._native_memory_ops_support():raise MemoryRequestRejectedError('document vector profile unavailable')
        def current():
            try:
                st=os.stat(prepared.database_path,follow_symlinks=False)
                return (authority.current_account() is True and policy_current() is True and self._guarded_preparation is prepared
                    and self._owned_transport is transport and self._owned_generation is generation and transport.process is process
                    and self._owner_task is task and not task.done() and transport.current(generation) and self._native_memory_ops_ready()
                    and NATIVE_VECTOR_PROFILE_OPERATION in self._native_memory_ops_support() and loop.time()<deadline
                    and (st.st_dev,st.st_ino)==(prepared.device,prepared.inode))
            except Exception:return False
        async def read(phase,body):
            if not current():raise MemoryRequestRejectedError('document vector profile authority changed')
            value=await self._s07_owner_rpc(owner,workspace,{'phase':phase,'request':body},deadline,read_ticket={})
            if not current() or type(value) is not dict:raise MemoryRequestRejectedError('document vector profile unavailable')
            return value
        await _queued_owned_thread(prepared.assert_valid)
        head=await read('document_vector_head',{'version':1,'owner':owner,'account_id':authority.account,'after':None,'expected_watermark':None,'limit':1})
        pointer=head.get('expected_pointer')
        if (set(head)!={'version','state','source_watermark','row_count','expected_pointer','descriptor'} or head.get('version')!=1
                or head.get('state')!='available' or type(head.get('row_count')) is not int or head['row_count']<1
                or type(head.get('source_watermark')) is not str or re.fullmatch(r'[0-9a-f]{64}',head['source_watermark']) is None
                or type(pointer) is not dict or set(pointer)!={'generation_id','publication_id','publication_watermark'}
                or any(type(value) is not str or not value or len(value)>512 for value in pointer.values())
                or pointer.get('publication_watermark')!=head.get('source_watermark')):
            raise MemoryRequestRejectedError('current document vector generation unavailable')
        original={'version':1,'owner':owner,'account_id':authority.account,'expected_pointer':pointer,'choice':selected_choice}
        start=await read('document_vector_selected_start',{'original':original,'policy':policy})
        if (set(start)!={'version','state','handle','profile_id'} or start.get('version')!=1
                or start.get('state') not in ('building','built') or type(start.get('handle')) is not str
                or re.fullmatch(r'vph_[0-9a-f]{32}',start['handle']) is None
                or type(start.get('profile_id')) is not str or re.fullmatch(r'[0-9a-f]{64}',start['profile_id']) is None):
            raise MemoryRequestRejectedError('document vector profile unavailable')
        wrapped={'original':original,'policy':policy,'handle':start['handle']}
        status=start
        while status.get('state')=='building':
            if not current():raise MemoryRequestRejectedError('document vector profile outcome unavailable')
            await asyncio.sleep(.05)
            status=await read('document_vector_selected_status',wrapped)
            if (set(status)!={'version','state','handle','profile_id'} or status.get('version')!=1
                    or status.get('state') not in ('building','built') or status.get('handle')!=start['handle']
                    or status.get('profile_id')!=start['profile_id']):
                raise MemoryRequestRejectedError('document vector profile outcome unavailable')
        if not current():raise MemoryRequestRejectedError('document vector profile authority changed')
        value=await self._host_candidate_request(owner,workspace,{'phase':'document_vector_selected_commit','request':wrapped},require_live=True,
            write_deadline=deadline,write_status={},before_issue=current)
        fields={'version','state','profile_id','generation_id','family','representation_sha256'}
        if (not current() or type(value) is not dict or set(value)-{'payload_bytes'}!=fields
                or value.get('version')!=1 or value.get('state')!='available' or value.get('profile_id')!=start['profile_id']
                or value.get('generation_id')!=pointer['generation_id'] or value.get('family')!=selected_choice['family']
                or type(value.get('representation_sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}',value['representation_sha256']) is None):
            raise MemoryRequestRejectedError('document vector profile outcome unavailable')
        if 'payload_bytes' in value:
            sizes=value['payload_bytes']
            dimensions=head.get('descriptor')
            dimension=dimensions.get('dimension') if type(dimensions) is dict else None
            if (type(sizes) is not dict or set(sizes)!={'original_vectors','derived_profile','profile_descriptor'}
                    or type(dimension) is not int or not 1<=dimension<=1024
                    or any(type(count) is not int or not 1<=count<=16*1024*1024 for count in sizes.values())
                    or sizes['original_vectors']!=head['row_count']*dimension*4 or sizes['profile_descriptor']>8192):
                # Older builds and invalid optional metadata still leave an
                # otherwise verified profile commit ready, with no byte claim.
                value={key: value[key] for key in fields}
        return value


    async def _host_document_vector_search(self, authority, client, query, *, deadline):
        """Return only Native ranks under the actual sealed chat admission."""
        from src.embeddings import ManagedEmbeddingClient
        from src.openclank.document_vector_access import _ChatDocumentSearch
        from src.openclank.mimo_supervisor import _queued_owned_thread
        if (type(authority) is not _ChatDocumentSearch or authority.current() is not True
                or type(client) is not ManagedEmbeddingClient or client.owner!=authority.owner
                or type(query) is not str or not query or len(query.encode('utf-8'))>65536
                or type(deadline) is not float or not math.isfinite(deadline) or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('document vector search unavailable')
        owner=authority.owner;workspace=self._workspace_id;prepared=self._guarded_preparation
        transport=self._owned_transport;generation=self._owned_generation;process=transport.process;task=self._owner_task
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.openclank.document_vector_settings import selected,choice
        with PREFERENCE_LOCK:selection=selected(_load_for_user(owner))
        selected_choice=choice(selection);policy=None;policy_current=lambda: True
        if selected_choice is not None:
            selection,policy,policy_current=self._document_vector_selection_context(authority)
            selected_choice=choice(selection)
            if (not self._native_memory_ops_ready() and not await self._host_negotiate_native_memory_ops(owner=owner,workspace_id=workspace,deadline=deadline)):
                selected_choice=None
            elif NATIVE_VECTOR_PROFILE_OPERATION not in self._native_memory_ops_support():selected_choice=None
        def current():
            try:
                stat_result=os.stat(prepared.database_path,follow_symlinks=False)
                return (authority.current() is True and policy_current() is True and self._guarded_preparation is prepared and self._owned_transport is transport
                    and self._owned_generation is generation and transport.process is process and self._owner_task is task
                    and not task.done() and transport.current(generation) and self._scope(owner).owner==owner
                    and (stat_result.st_dev,stat_result.st_ino)==(prepared.device,prepared.inode))
            except Exception:return False
        async def read(phase,request):
            if not current():raise MemoryRequestRejectedError('document vector admission changed')
            value=await self._s07_owner_rpc(owner,workspace,{'phase':phase,'request':request},deadline,read_ticket={})
            if not current() or type(value) is not dict:raise MemoryRequestRejectedError('document vector search unavailable')
            return value
        await _queued_owned_thread(prepared.assert_valid)
        head=await read('document_vector_head',{'version':1,'owner':owner,'account_id':authority.account,'after':None,'expected_watermark':None,'limit':1})
        if (set(head)!={'version','state','source_watermark','row_count','expected_pointer','descriptor'} or head.get('version')!=1
                or head.get('state')!='available' or type(head.get('source_watermark')) is not str or re.fullmatch(r'[0-9a-f]{64}',head['source_watermark']) is None
                or type(head.get('row_count')) is not int or not 1<=head['row_count']<=100000):
            raise MemoryRequestRejectedError('document vector head unavailable')
        pointer=head['expected_pointer'];descriptor=head['descriptor']
        if (type(pointer) is not dict or set(pointer)!={'generation_id','publication_id','publication_watermark'}
                or any(type(x) is not str or not x or len(x)>512 for x in pointer.values())
                or pointer['publication_watermark']!=head['source_watermark']
                or type(descriptor) is not dict or set(descriptor)!={'provider_ref','model','endpoint_class','dimension'}
                or any(type(descriptor[x]) is not str or not descriptor[x] for x in ('provider_ref','model','endpoint_class'))
                or type(descriptor['dimension']) is not int or not 1<=descriptor['dimension']<=65536):
            raise MemoryRequestRejectedError('document vector head unavailable')
        key=lambda suffix:'dvs_'+hashlib.sha256((authority.operation_id+'\0'+suffix).encode()).hexdigest()
        await client._encode_owned(['dimension probe'],idempotency_key=key('probe'),deadline=deadline,before_issue=current)
        dimension=client._dim
        if not current() or (client.provider_ref,client.model,client.endpoint_class,dimension)!=(descriptor['provider_ref'],descriptor['model'],descriptor['endpoint_class'],descriptor['dimension']):
            raise MemoryRequestRejectedError('document vector route changed')
        encoded=await client._encode_owned([query],idempotency_key=key('query'),deadline=deadline,before_issue=current)
        if not current() or len(encoded)!=1:raise MemoryRequestRejectedError('document vector query unavailable')
        vector=[float(v) for v in encoded[0]]
        if len(vector)!=dimension or any(not math.isfinite(v) for v in vector):raise MemoryRequestRejectedError('document vector query unavailable')
        limit=min(10000,head['row_count']);backend='exact';approximate=False;audit=None;strategy=None
        if selected_choice is None:
            request={'version':1,'owner':owner,'account_id':authority.account,'expected_pointer':pointer,**descriptor,'query':vector,'limit':limit}
            result=await read('document_vector_search',request)
            valid_keys={'version','state','hits','truncated'}
        else:
            family=selection['family']
            effort=selection['effort'] if family in ('ivf_flat','ivf_minibatch','hnsw') else 1
            visits=selection['visit_budget'] if family in ('ivf_flat','ivf_minibatch','hnsw') else 1
            candidates=selection['candidate_limit'] if family in ('scalar8','pq') else 1
            if family in ('scalar8','pq'):limit=min(limit,candidates)
            request={'original':{'version':1,'owner':owner,'account_id':authority.account,'expected_pointer':pointer,'choice':selected_choice},
                'policy':policy,'profile_id':'','query':vector,'limit':limit,'effort':effort,'visit_budget':visits,'candidate_limit':candidates,'selector':None}
            result=await read('document_vector_selected_search',request)
            valid_keys={'version','state','hits','truncated','backend','approximate'}
            if 'audit' in result:
                audit=result['audit'];valid_keys.add('audit')
                if (family not in ('ivf_flat','ivf_minibatch','hnsw') or type(audit) is not dict
                        or set(audit)!={'visited','visit_budget_exhausted','result_limit'}
                        or type(audit['visited']) is not int or not 0<=audit['visited']<=visits
                        or type(audit['visit_budget_exhausted']) is not bool or type(audit['result_limit']) is not bool
                        or result.get('truncated') is not (audit['visit_budget_exhausted'] or audit['result_limit'])
                        or (audit['visit_budget_exhausted'] and audit['visited']!=visits)
                        or (result.get('backend')=='exact_fallback' and (audit['visited']!=0 or audit['visit_budget_exhausted']))):
                    raise MemoryRequestRejectedError('document vector result unavailable')
            backend=result.get('backend');approximate=result.get('approximate')
            if backend not in (family,'exact_fallback') or type(approximate) is not bool or approximate!=(backend==family):
                raise MemoryRequestRejectedError('document vector result unavailable')
            if 'strategy' in result:
                strategy=result['strategy'];valid_keys.add('strategy')
                if type(strategy) is not dict or set(strategy)!={'reason','eligible_vectors','candidate_scoring','candidate_count'}:
                    raise MemoryRequestRejectedError('document vector strategy unavailable')
                count=strategy['eligible_vectors']; candidates=strategy['candidate_count']
                reason=strategy['reason']; scoring=strategy['candidate_scoring']
                if (type(count) is not int or count!=head['row_count']
                        or (candidates is not None and (type(candidates) is not int or not 0<=candidates<=count))
                        or (approximate and reason!='profile_approximate')
                        or (not approximate and reason not in ('representation_missing','exact_within_visit_budget'))
                        or (reason=='exact_within_visit_budget' and (family not in ('ivf_flat','ivf_minibatch','hnsw') or count>visits))
                        or (approximate and family in ('scalar8','pq') and (scoring!='original_rerank' or candidates is None))
                        or (approximate and family in ('ivf_flat','ivf_minibatch','hnsw') and (scoring!='original' or candidates is not None))
                        or (not approximate and (scoring!='original' or candidates!=count))):
                    raise MemoryRequestRejectedError('document vector strategy unavailable')
        if (set(result)!=valid_keys or result.get('version')!=1 or result.get('state')!='ranked'
                or type(result.get('truncated')) is not bool or type(result.get('hits')) is not list or len(result['hits'])>limit):
            raise MemoryRequestRejectedError('document vector result unavailable')
        seen=set();previous=None
        for item in result['hits']:
            if (type(item) is not dict or set(item)!={'chunk_id','score'} or type(item['chunk_id']) is not str
                    or re.fullmatch(r'chunk_[0-9a-f]{32}',item['chunk_id']) is None or item['chunk_id'] in seen
                    or type(item['score']) not in (int,float) or not math.isfinite(item['score']) or item['score']<=0):
                raise MemoryRequestRejectedError('document vector result unavailable')
            sort=(-item['score'],item['chunk_id'])
            if previous is not None and sort<previous:raise MemoryRequestRejectedError('document vector result unavailable')
            previous=sort;seen.add(item['chunk_id'])
        if not current():raise MemoryRequestRejectedError('document vector admission changed')
        if strategy is not None:
            audit={**(audit or {'visited':0,'visit_budget_exhausted':False,'result_limit':result['truncated']}),
                   'strategy':strategy}
        return authority._seal_ranking(pointer,head['source_watermark'],result['hits'],result['truncated'],backend,approximate,policy_current,audit)

    def _document_vector_intent_admission(self, authority, client, operation_id, deadline, *, version=1):
        from routes.personal_routes import _PersonalDocumentAuthority
        from src.embeddings import ManagedEmbeddingClient
        if (type(authority) is not _PersonalDocumentAuthority or authority.snapshot is not None or authority.current_account() is not True
                or type(client) is not ManagedEmbeddingClient or client.owner!=authority.owner or client.root_operation_id!=operation_id
                or type(operation_id) is not str or re.fullmatch(r'[A-Za-z0-9_-]{16,128}',operation_id) is None
                or type(deadline) is not float or not math.isfinite(deadline) or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('document vector intent unavailable')
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;process=transport.process;task=self._owner_task
        if getattr(prepared,'_document_vector_intent_marker',None)!=('1',):raise MemoryRequestRejectedError('document vector intent unavailable')
        if version not in (1,2) or (version==2 and getattr(prepared,'_document_vector_batch_marker',None)!=('1',)):raise MemoryRequestRejectedError('document vector batch profile unavailable')
        def current():
            try:
                st=os.stat(prepared.database_path,follow_symlinks=False)
                return (authority.current_account() is True and self._guarded_preparation is prepared and self._owned_transport is transport
                    and self._owned_generation is generation and transport.process is process and self._owner_task is task and not task.done()
                    and transport.current(generation) and self._scope(authority.owner).owner==authority.owner
                    and (st.st_dev,st.st_ino)==(prepared.device,prepared.inode) and asyncio.get_running_loop().time()<deadline)
            except Exception:return False
        return prepared,current

    async def _document_vector_probe_prepared(self, authority, client, operation_id, current):
        from src.openclank.operation_router import ManagedOperationRequest, _thaw_json, get_operation_router
        if current() is not True or client.content_purpose!='index':raise MemoryRequestRejectedError('document vector authority changed')
        key='dv_'+hashlib.sha256((operation_id+'\0probe').encode()).hexdigest();router=get_operation_router()
        request=ManagedOperationRequest(owner=authority.owner,operation='embeddings.create',purpose='embeddings',
            input={'texts':['dimension probe'],'purpose':'index'},model_route_id=client.model_route_id,
            root_operation_id=operation_id,idempotency_key=key)
        prepared=await asyncio.to_thread(router.prepare,request);payload=_thaw_json(prepared.payload)
        if (current() is not True or type(payload) is not dict
                or set(payload)!={'rootOperationID','idempotencyKey','operation','routes','input','artifactInputs','options'}
                or payload['rootOperationID']!=operation_id or payload['idempotencyKey']!=key or payload['operation']!='embeddings.create'
                or payload['input']!={'texts':['dimension probe'],'purpose':'index'} or payload['artifactInputs']!=[] or payload['options']!={}
                or type(payload['routes']) is not list or len(payload['routes'])!=1
                or len(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode())>8192):
            raise MemoryRequestRejectedError('document vector probe unavailable')
        return router,prepared,payload,request

    async def _host_document_vector_intent_prepare(self, authority, client, *, operation_id, deadline, version=1):
        from src.openclank.mimo_supervisor import _queued_owned_thread
        prepared,current=self._document_vector_intent_admission(authority,client,operation_id,deadline,version=version)
        await _queued_owned_thread(prepared.assert_valid)
        head=await self._s07_owner_rpc(authority.owner,self._workspace_id,{'phase':'document_vector_head','request':{'version':1,'owner':authority.owner,'account_id':authority.account,'after':None,'expected_watermark':None,'limit':1}},deadline,read_ticket={})
        if (current() is not True or type(head) is not dict or set(head)!={'version','state','source_watermark','row_count','expected_pointer','descriptor'}
                or head.get('version')!=1 or head.get('state')!='available' or type(head.get('row_count')) is not int or not 1<=head['row_count']<=100000
                or type(head.get('source_watermark')) is not str or re.fullmatch(r'[0-9a-f]{64}',head['source_watermark']) is None):
            raise MemoryRequestRejectedError('document vector intent unavailable')
        pointer=head['expected_pointer']
        if pointer is not None and (type(pointer) is not dict or set(pointer)!={'generation_id','publication_id','publication_watermark'} or any(type(x) is not str or not x or len(x)>512 for x in pointer.values())):
            raise MemoryRequestRejectedError('document vector intent unavailable')
        _router,_probe,payload,_request=await self._document_vector_probe_prepared(authority,client,operation_id,current)
        original={'version':version,'account_id':authority.account,'captured_owner':authority.owner,'operation_id':operation_id,'workspace_id':'','project_id':'',
            'head':{'source_watermark':head['source_watermark'],'row_count':head['row_count'],'expected_pointer':pointer},'probe':payload}
        result=await self._host_candidate_request(authority.owner,self._workspace_id,{'phase':'document_vector_intent_prepare','request':original},require_live=True,
            write_deadline=deadline,write_status={},before_issue=current)
        expected={'version':1,'state':'queued','operation_id':operation_id}
        if version==2:
            retained=result.get('original') if type(result) is dict else None
            if (type(retained) is not dict or set(retained)!=set(original)|{'source_epoch'} or type(retained.get('source_epoch')) is not int
                    or retained['source_epoch']<1 or {k:retained[k] for k in original}!=original or set(result)!=set(expected)|{'original'}):
                raise MemoryRequestRejectedError('document vector intent outcome unavailable')
            result={k:result[k] for k in expected}
        if current() is not True or result!=expected:raise MemoryRequestRejectedError('document vector intent outcome unavailable')
        return result

    async def _host_document_vector_intent_original(self, authority, client, *, operation_id, deadline, version=1):
        from src.openclank.mimo_supervisor import _queued_owned_thread
        prepared,current=self._document_vector_intent_admission(authority,client,operation_id,deadline,version=version)
        await _queued_owned_thread(prepared.assert_valid)
        identity={'version':1,'account_id':authority.account,'owner':authority.owner,'operation_id':operation_id}
        result=await self._s07_owner_rpc(authority.owner,self._workspace_id,{'phase':'document_vector_intent_status','request':identity},deadline,read_ticket={})
        if current() is not True or type(result) is not dict or result.get('version')!=1 or result.get('state') not in (('queued','unknown','published','cancelled','terminal') if version==2 else ('queued','unknown','published','cancelled')):
            raise MemoryRequestRejectedError('document vector intent unavailable')
        if result['state']=='terminal':
            if set(result)!={'version','state','operation_id','terminal'} or result.get('operation_id')!=operation_id or result.get('terminal') not in ('published','cancelled'):
                raise MemoryRequestRejectedError('document vector terminal unavailable')
            return result,None,None,None,current,identity
        if result['state']=='cancelled':
            if set(result)!={'version','state','operation_id','paid_outcome','claim_state'} or result['operation_id']!=operation_id or result['paid_outcome']!='unknown' or result['claim_state'] not in ('unclaimed','claimed'):
                raise MemoryRequestRejectedError('document vector intent unavailable')
            return result,None,None,None,current,identity
        if set(result)!=({'version','state','original','outcome'} if result['state']=='published' else {'version','state','original'}):
            raise MemoryRequestRejectedError('document vector intent unavailable')
        original=result.get('original')
        if (type(original) is not dict or set(original)!=({'version','account_id','captured_owner','operation_id','workspace_id','project_id','head','probe'} | ({'source_epoch'} if version==2 else set()))
                or original.get('version')!=version or (version==2 and (type(original.get('source_epoch')) is not int or original['source_epoch']<1)) or original.get('account_id')!=authority.account or original.get('captured_owner')!=authority.owner
                or original.get('operation_id')!=operation_id or original.get('workspace_id')!='' or original.get('project_id')!=''):
            raise MemoryRequestRejectedError('document vector intent unavailable')
        router,probe,payload,request=await self._document_vector_probe_prepared(authority,client,operation_id,current)
        if payload!=original['probe'] or current() is not True:raise MemoryRequestRejectedError('document vector route changed')
        return result,router,probe,request,current,identity

    async def _host_document_vector_intent_status(self, authority, client, *, operation_id, deadline, version=1):
        from src.openclank.operation_router import _thaw_json
        result,router,probe,request,current,_identity=await self._host_document_vector_intent_original(authority,client,operation_id=operation_id,deadline=deadline,version=version)
        if result['state']=='terminal':return result
        if result['state']=='cancelled':return {'version':1,'state':'cancelled','operation_id':operation_id,'paid_outcome':'unknown'}
        if result['state']=='published':return {'version':1,'state':'published','operation_id':operation_id,'outcome':result['outcome']}
        if result['state']=='queued':return {'version':1,'state':'queued','operation_id':operation_id}
        recovered=await asyncio.to_thread(router.recover_prepared,probe,authenticated_owner=authority.owner)
        try:route_current=current() is True and _thaw_json(router.prepare(request).payload)==result['original']['probe']
        except Exception:route_current=False
        if route_current is not True:raise MemoryRequestRejectedError('document vector authority changed')
        if type(recovered) is not dict or recovered.get('state')!='terminal':return {'version':1,'state':'unknown','operation_id':operation_id}
        terminal=recovered.get('result')
        if terminal is None or terminal.operation!='embeddings.create' or terminal.root_operation_id!=operation_id:
            raise MemoryRequestRejectedError('document vector outcome unavailable')
        return {'version':1,'state':'probe_complete' if terminal.state=='complete' else 'probe_terminal','operation_id':operation_id}

    async def _host_document_vector_intent_execute(self, authority, client, *, operation_id, deadline):
        from src.openclank.operation_router import _thaw_json
        result,router,probe,probe_request,current,identity=await self._host_document_vector_intent_original(authority,client,operation_id=operation_id,deadline=deadline)
        if result['state']!='queued':raise MemoryRequestRejectedError('document vector original already claimed; check status')
        original=result['original']
        def route_current():
            try:
                return current() is True and _thaw_json(router.prepare(probe_request).payload)==original['probe']
            except Exception:return False
        claimed=await self._host_candidate_request(authority.owner,self._workspace_id,{'phase':'document_vector_intent_claim','request':identity},require_live=True,
            write_deadline=deadline,write_status={},before_issue=route_current)
        if route_current() is not True or claimed!={'version':1,'state':'claimed','original':original}:
            raise MemoryRequestRejectedError('document vector claim outcome unavailable')
        return await self._host_document_vector_build(authority,client,operation_id=operation_id,deadline=deadline,
            retained_intent=original,prepared_original=probe,route_current=route_current)


    async def _host_document_vector_v2_capability(self, authority, client, *, operation_id, deadline):
        """Check the admitted store, current document scope and configured route without issuing a frame."""
        from src.openclank.mimo_supervisor import _queued_owned_thread
        prepared,current=self._document_vector_intent_admission(authority,client,operation_id,deadline,version=2)
        await _queued_owned_thread(prepared.assert_valid)
        from src.openclank.operation_router import ManagedOperationError
        from src.embeddings import ManagedEmbeddingError
        try:
            await self._document_vector_probe_prepared(authority,client,operation_id,current);route_ready=True
        except (ManagedOperationError,ManagedEmbeddingError,ValueError):
            route_ready=False
        if current() is not True:raise MemoryRequestRejectedError('document vector authority changed')
        request={'version':1,'owner':authority.owner,'account_id':authority.account,'after':None,'expected_watermark':None,'limit':1}
        head=await self._s07_owner_rpc(authority.owner,self._workspace_id,{'phase':'document_vector_head','request':request},deadline,read_ticket={})
        if (current() is not True or type(head) is not dict or set(head)!={'version','state','source_watermark','row_count','expected_pointer','descriptor'}
                or head.get('version')!=1 or head.get('state')!='available' or type(head.get('row_count')) is not int or not 0<=head['row_count']<=100000
                or type(head.get('source_watermark')) is not str or re.fullmatch(r'[0-9a-f]{64}',head['source_watermark']) is None):
            raise MemoryRequestRejectedError('document vector capability unavailable')
        return {'version':1,'state':'ready','row_count':head['row_count'],'account_id':authority.account,'workspace_id':self._workspace_id,'profile':'document_vector_batch.v2','route_ready':route_ready}

    async def _host_document_vector_v2_cancel(self, authority, client, *, operation_id, deadline):
        """Retire an exact original without treating a claimed outcome as unsent."""
        from src.openclank.mimo_supervisor import _queued_owned_thread
        prepared,current=self._document_vector_intent_admission(authority,client,operation_id,deadline,version=2)
        await _queued_owned_thread(prepared.assert_valid)
        identity={'version':1,'account_id':authority.account,'owner':authority.owner,'operation_id':operation_id}
        result=await self._host_candidate_request(authority.owner,self._workspace_id,{'phase':'document_vector_intent_cancel','request':identity},require_live=True,
            write_deadline=deadline,write_status={},before_issue=current)
        if (current() is not True or type(result) is not dict or set(result)!={'version','state','operation_id','paid_outcome','claim_state'}
                or result.get('version')!=1 or result.get('state')!='cancelled' or result.get('operation_id')!=operation_id
                or result.get('paid_outcome')!='unknown' or result.get('claim_state') not in ('unclaimed','claimed')):
            raise MemoryRequestRejectedError('document vector cancellation unavailable')
        return {'version':2,'state':'cancelled','operation_id':operation_id,'paid_outcome':'unknown'}

    async def _host_document_vector_v2_step(self, authority, client, *, operation_id, deadline, action):
        """Explicit v2 execution/reconciliation under the existing document lane."""
        from src.openclank.operation_router import ManagedOperationRequest, _thaw_json
        if action not in ('execute','resume'):
            raise MemoryRequestRejectedError('document vector action unavailable')
        result,router,probe,probe_request,current,identity=await self._host_document_vector_intent_original(
            authority,client,operation_id=operation_id,deadline=deadline,version=2)
        if result['state']=='terminal':return {'version':2,'state':'terminal','operation_id':operation_id,'terminal':result['terminal']}
        if result['state']=='cancelled':return {'version':2,'state':'cancelled','operation_id':operation_id,'paid_outcome':'unknown'}
        if result['state']=='published':return {'version':2,'state':'published','operation_id':operation_id,'outcome':result['outcome']}
        original=result['original']
        def route_current():
            try:return current() is True and _thaw_json(router.prepare(probe_request).payload)==original['probe']
            except Exception:return False
        if result['state']=='queued':
            claimed=await self._host_candidate_request(authority.owner,self._workspace_id,{'phase':'document_vector_intent_claim','request':identity},require_live=True,
                write_deadline=deadline,write_status={},before_issue=route_current)
            if route_current() is not True or claimed!={'version':1,'state':'claimed','original':original}:
                raise MemoryRequestRejectedError('document vector claim outcome unavailable')
            probe_key='dv_'+hashlib.sha256((operation_id+'\0probe').encode()).hexdigest()
            try:
                await client._encode_owned(['dimension probe'],idempotency_key=probe_key,deadline=deadline,before_issue=route_current,prepared_original=probe)
            except Exception:
                if route_current() is not True:raise MemoryRequestRejectedError('document vector authority changed')
                return {'version':2,'state':'unknown','operation_id':operation_id}
        else:
            recovered=await asyncio.to_thread(router.recover_prepared,probe,authenticated_owner=authority.owner)
            if route_current() is not True:raise MemoryRequestRejectedError('document vector authority changed')
            if type(recovered) is not dict or recovered.get('state')!='terminal':
                return {'version':2,'state':'unknown','operation_id':operation_id}
            terminal=recovered.get('result')
            if terminal is None or terminal.operation!='embeddings.create' or terminal.root_operation_id!=operation_id or terminal.state!='complete':
                return {'version':2,'state':'unavailable','operation_id':operation_id}
            client._accept_owned_terminal(terminal,expected_count=1,before_issue=route_current)
        dimension=client._dim;head=original['head']
        if (route_current() is not True or type(dimension) is not int or not 1<=dimension<=65536
                or type(head) is not dict or set(head)!={'source_watermark','row_count','expected_pointer'}
                or type(head.get('row_count')) is not int or not 1<=head['row_count']<=100000
                or type(head.get('source_watermark')) is not str or re.fullmatch(r'[0-9a-f]{64}',head['source_watermark']) is None
                or (head.get('expected_pointer') is not None and (type(head['expected_pointer']) is not dict
                    or set(head['expected_pointer'])!={'generation_id','publication_id','publication_watermark'}
                    or any(type(v) is not str or not v or len(v)>512 for v in head['expected_pointer'].values())))):
            raise MemoryRequestRejectedError('document vector source unavailable')
        descriptor={'version':1,'owner':authority.owner,'account_id':authority.account,'operation_id':operation_id,
            'provider_ref':client.provider_ref,'model':client.model,'endpoint_class':client.endpoint_class,'dimension':dimension,
            'source_watermark':head['source_watermark'],'row_count':head['row_count'],'expected_pointer':head['expected_pointer']}
        async def write(phase,request):
            if route_current() is not True:raise MemoryRequestRejectedError('document vector authority changed')
            out=await self._host_candidate_request(authority.owner,self._workspace_id,{'phase':phase,'request':request},require_live=True,
                write_deadline=deadline,write_status={},before_issue=route_current)
            if route_current() is not True or type(out) is not dict:raise MemoryRequestRejectedError('document vector outcome unavailable')
            return out
        async def read(phase,request):
            if route_current() is not True:raise MemoryRequestRejectedError('document vector authority changed')
            out=await self._s07_owner_rpc(authority.owner,self._workspace_id,{'phase':phase,'request':request},deadline,read_ticket={})
            if route_current() is not True or type(out) is not dict:raise MemoryRequestRejectedError('document vector outcome unavailable')
            return out
        started=await write('document_vector_batch_begin',descriptor)
        if set(started)!={'version','state','stage_id'} or started.get('version')!=1 or started.get('state')!='building' or type(started.get('stage_id')) is not str or re.fullmatch(r'stage_vec_[0-9a-f]{64}',started['stage_id']) is None:
            raise MemoryRequestRejectedError('document vector stage unavailable')
        stage=started['stage_id']
        while True:
            state=await read('document_vector_batch_status',descriptor)
            if state.get('version')!=2 or state.get('stage_id')!=stage or type(state.get('offset')) is not int:
                raise MemoryRequestRejectedError('document vector batch unavailable')
            if state.get('state')=='ready_to_finalize':
                if set(state)!={'version','state','stage_id','offset'}:raise MemoryRequestRejectedError('document vector batch unavailable')
                result=await write('document_vector_batch_finalize',descriptor)
                if (set(result)!={'version','state','generation_id','publication_id','source_watermark','current'} or result.get('version')!=1
                        or result.get('state')!='published' or result.get('current') is not True or result.get('source_watermark')!=head['source_watermark']
                        or type(result.get('generation_id')) is not str or re.fullmatch(r'gen_vec_[0-9a-f]{64}',result['generation_id']) is None
                        or type(result.get('publication_id')) is not str or re.fullmatch(r'pub_vec_[0-9a-f]{64}',result['publication_id']) is None):
                    raise MemoryRequestRejectedError('document vector publication unavailable')
                return result
            was_unknown=state.get('state')=='unknown'
            if state.get('state')=='ready_to_claim':
                if set(state)!={'version','state','stage_id','offset'}:raise MemoryRequestRejectedError('document vector batch unavailable')
                expected_offset=state['offset'];state=await write('document_vector_batch_claim',descriptor)
                if state.get('state')!='claimed' or state.get('offset')!=expected_offset:raise MemoryRequestRejectedError('document vector claim unavailable')
            elif not was_unknown:raise MemoryRequestRejectedError('document vector batch unavailable')
            if (set(state)!={'version','state','stage_id','offset','claim_sha256','request_sha256','items'} or state.get('version')!=2
                    or state.get('stage_id')!=stage or type(state.get('offset')) is not int or state['offset']<0
                    or any(type(state.get(k)) is not str or re.fullmatch(r'[0-9a-f]{64}',state[k]) is None for k in ('claim_sha256','request_sha256'))
                    or type(state.get('items')) is not list or not 1<=len(state['items'])<=16):
                raise MemoryRequestRejectedError('document vector claim unavailable')
            selected=state['items'];texts=[]
            for item in selected:
                if (type(item) is not dict or set(item)!={'chunk_id','content_hash','source_id','source_revision','text'}
                        or type(item.get('chunk_id')) is not str or re.fullmatch(r'chunk_[0-9a-f]{32}',item['chunk_id']) is None
                        or type(item.get('source_id')) is not str or re.fullmatch(r'src_[0-9a-f]{32}',item['source_id']) is None
                        or type(item.get('content_hash')) is not str or re.fullmatch(r'[0-9a-f]{64}',item['content_hash']) is None
                        or type(item.get('source_revision')) is not int or item['source_revision']<1 or type(item.get('text')) is not str
                        or hashlib.sha256(item['text'].encode()).hexdigest()!=item['content_hash']):
                    raise MemoryRequestRejectedError('document vector source unavailable')
                texts.append(item['text'])
            offset=state['offset'];key='dv_'+hashlib.sha256((operation_id+'\0batch:'+str(offset)).encode()).hexdigest()
            request=ManagedOperationRequest(owner=authority.owner,operation='embeddings.create',purpose='embeddings',input={'texts':texts,'purpose':'index'},
                model_route_id=client.model_route_id,root_operation_id=operation_id,idempotency_key=key)
            prepared=await asyncio.to_thread(router.prepare,request);payload=_thaw_json(prepared.payload)
            if (type(payload) is not dict or set(payload)!={'rootOperationID','idempotencyKey','operation','routes','input','artifactInputs','options'}
                    or payload.get('operation')!='embeddings.create' or payload.get('input')!={'texts':texts,'purpose':'index'}
                    or payload.get('artifactInputs')!=[] or payload.get('options')!={}):
                raise MemoryRequestRejectedError('document vector request unavailable')
            material={key:payload[key] for key in ('operation','routes','input','artifactInputs','options')}
            digest=hashlib.sha256(json.dumps(material,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
            if (route_current() is not True or payload.get('rootOperationID')!=operation_id or payload.get('idempotencyKey')!=key
                    or payload.get('routes')!=original['probe']['routes'] or digest!=state['request_sha256']):
                raise MemoryRequestRejectedError('document vector request changed')
            if was_unknown:
                recovered=await asyncio.to_thread(router.recover_prepared,prepared,authenticated_owner=authority.owner)
                if route_current() is not True:raise MemoryRequestRejectedError('document vector authority changed')
                if type(recovered) is not dict or recovered.get('state')!='terminal':return {'version':2,'state':'unknown','operation_id':operation_id}
                terminal=recovered.get('result')
                if terminal is None or terminal.operation!='embeddings.create' or terminal.root_operation_id!=operation_id or terminal.state!='complete':
                    return {'version':2,'state':'unavailable','operation_id':operation_id}
                encoded=client._accept_owned_terminal(terminal,expected_count=len(selected),before_issue=route_current)
            else:
                try:
                    encoded=await client._encode_owned(texts,idempotency_key=key,deadline=deadline,before_issue=route_current,prepared_original=prepared)
                except Exception:
                    if route_current() is not True:raise MemoryRequestRejectedError('document vector authority changed')
                    return {'version':2,'state':'unknown','operation_id':operation_id}
            rows=[{'chunk_id':item['chunk_id'],'content_hash':item['content_hash'],'source_revision':item['source_revision'],
                   'values':[float(v) for v in vector]} for item,vector in zip(selected,encoded)]
            if len(rows)!=len(selected) or any(len(row['values'])!=dimension or any(not math.isfinite(v) for v in row['values']) for row in rows):
                raise MemoryRequestRejectedError('document vector result unavailable')
            outcome=await write('document_vector_batch_complete',{'original':descriptor,'offset':offset,'rows':rows})
            if (set(outcome)!={'version','state','stage_id','offset','count','terminal_sha256'} or outcome.get('version')!=2
                    or outcome.get('state')!='complete' or outcome.get('stage_id')!=stage or outcome.get('offset')!=offset or outcome.get('count')!=len(rows)
                    or type(outcome.get('terminal_sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}',outcome['terminal_sha256']) is None):
                raise MemoryRequestRejectedError('document vector terminal unavailable')
            if was_unknown or action=='resume':return {'version':2,'state':'ready_to_continue','operation_id':operation_id}


    async def _host_document_vector_build(self, authority, client, *, operation_id, deadline, retained_intent=None, prepared_original=None, route_current=None):
        """One explicitly admitted managed build; uncertainty is never retried here."""
        from routes.personal_routes import _PersonalDocumentAuthority
        from src.embeddings import ManagedEmbeddingClient
        from src.openclank.mimo_supervisor import _queued_owned_thread
        if (type(authority) is not _PersonalDocumentAuthority or authority.snapshot is not None
                or authority.current_account() is not True or type(client) is not ManagedEmbeddingClient
                or client.owner!=authority.owner or client.root_operation_id!=operation_id
                or type(operation_id) is not str or re.fullmatch(r'[A-Za-z0-9_-]{16,128}',operation_id) is None
                or type(deadline) is not float or not math.isfinite(deadline) or not self._host_bounded_write_available()
                or type(retained_intent) is not dict or not callable(route_current) or prepared_original is None):
            raise MemoryRequestRejectedError('document vector build unavailable')
        loop=asyncio.get_running_loop();owner=authority.owner;workspace=self._workspace_id;prepared=self._guarded_preparation
        transport=self._owned_transport;generation=self._owned_generation;process=transport.process;task=self._owner_task
        def current():
            try:
                stat_result=os.stat(prepared.database_path,follow_symlinks=False)
                return (authority.current_account() is True and route_current() is True and self._guarded_preparation is prepared
                    and self._owned_transport is transport and self._owned_generation is generation
                    and transport.process is process and self._owner_task is task and not task.done()
                    and transport.current(generation) and self._scope(owner).owner==owner
                    and (stat_result.st_dev,stat_result.st_ino)==(prepared.device,prepared.inode)
                    and loop.time()<deadline)
            except Exception:return False
        async def read(phase,request):
            if not current():raise MemoryRequestRejectedError('document vector admission changed')
            result=await self._s07_owner_rpc(owner,workspace,{'phase':phase,'request':request},deadline,read_ticket={})
            if not current() or type(result) is not dict:raise MemoryRequestRejectedError('document vector read unavailable')
            return result
        async def write(phase,request):
            if not current():raise MemoryRequestRejectedError('document vector admission changed')
            status={}
            result=await self._host_candidate_request(owner,workspace,{'phase':phase,'request':request},require_live=True,
                write_deadline=deadline,write_status=status,before_issue=current)
            if not current() or type(result) is not dict:raise MemoryRequestRejectedError('document vector outcome unavailable')
            return result
        await _queued_owned_thread(prepared.assert_valid)
        head_request={'version':1,'owner':owner,'account_id':authority.account,'after':None,'expected_watermark':None,'limit':1}
        head=await read('document_vector_head',head_request)
        if (set(head)!={'version','state','source_watermark','row_count','expected_pointer','descriptor'} or head.get('version')!=1
                or head.get('state')!='available' or type(head.get('row_count')) is not int or not 0<=head['row_count']<=100000
                or type(head.get('source_watermark')) is not str or re.fullmatch(r'[0-9a-f]{64}',head['source_watermark']) is None):
            raise MemoryRequestRejectedError('document vector head unavailable')
        pointer=head['expected_pointer']
        if retained_intent.get('head')!={'source_watermark':head['source_watermark'],'row_count':head['row_count'],'expected_pointer':pointer}:raise MemoryRequestRejectedError('document vector source changed')
        if pointer is not None and (type(pointer) is not dict or set(pointer)!={'generation_id','publication_id','publication_watermark'}
                or any(type(v) is not str or not v or len(v)>512 for v in pointer.values())):
            raise MemoryRequestRejectedError('document vector head unavailable')
        if head['row_count']==0:return {'version':1,'state':'empty'}
        key=lambda label: 'dv_'+hashlib.sha256((operation_id+'\0'+label).encode()).hexdigest()
        if not current():raise MemoryRequestRejectedError('document vector admission changed')
        await client._encode_owned(['dimension probe'],idempotency_key=key('probe'),deadline=deadline,before_issue=current,prepared_original=prepared_original)
        dimension=client._dim
        if not current() or type(dimension) is not int or not 1<=dimension<=65536:
            raise MemoryRequestRejectedError('document vector model unavailable')
        if await read('document_vector_head',head_request)!=head:raise MemoryRequestRejectedError('document vector source changed')
        original={'version':1,'owner':owner,'account_id':authority.account,'operation_id':operation_id,
            'provider_ref':client.provider_ref,'model':client.model,'endpoint_class':client.endpoint_class,
            'dimension':dimension,'source_watermark':head['source_watermark'],'row_count':head['row_count'],'expected_pointer':pointer}
        started=await write('document_vector_intent_begin',original)
        if set(started)!={'version','state','stage_id'} or started.get('version')!=1 or started.get('state')!='building' or type(started.get('stage_id')) is not str:
            raise MemoryRequestRejectedError('document vector begin unavailable')
        stage=started['stage_id'];batch=max(1,min(16,57000//(dimension*24+512)))
        for offset in range(0,head['row_count'],batch):
            selected=[]
            for index in range(offset,min(offset+batch,head['row_count'])):
                page=await read('document_vector_intent_text',{'original':original,'offset':index})
                if (set(page)!={'version','state','stage_id','offset','items'} or page.get('version')!=1 or page.get('state')!='available'
                        or page.get('stage_id')!=stage or page.get('offset')!=index or type(page.get('items')) is not list or len(page['items'])!=1):
                    raise MemoryRequestRejectedError('document vector text unavailable')
                item=page['items'][0]
                if (type(item) is not dict or set(item)!={'chunk_id','content_hash','source_revision','text'}
                        or type(item.get('chunk_id')) is not str or re.fullmatch(r'chunk_[0-9a-f]{32}',item['chunk_id']) is None
                        or type(item.get('content_hash')) is not str or re.fullmatch(r'[0-9a-f]{64}',item['content_hash']) is None
                        or type(item.get('source_revision')) is not int or item['source_revision']<1 or type(item.get('text')) is not str
                        or hashlib.sha256(item['text'].encode('utf-8')).hexdigest()!=item['content_hash']):
                    raise MemoryRequestRejectedError('document vector text unavailable')
                selected.append(item)
            if not current():raise MemoryRequestRejectedError('document vector admission changed')
            encoded=await client._encode_owned([item['text'] for item in selected],idempotency_key=key('batch:'+str(offset)),deadline=deadline,before_issue=current)
            if not current() or len(encoded)!=len(selected):raise MemoryRequestRejectedError('document vector model unavailable')
            rows=[{'chunk_id':item['chunk_id'],'content_hash':item['content_hash'],'source_revision':item['source_revision'],
                   'values':[float(v) for v in vector]} for item,vector in zip(selected,encoded)]
            if any(len(row['values'])!=dimension or any(not math.isfinite(v) for v in row['values']) for row in rows):
                raise MemoryRequestRejectedError('document vector model unavailable')
            result=await write('document_vector_intent_append',{'original':original,'offset':offset,'rows':rows})
            if result!={'version':1,'state':'building','stage_id':stage,'offset':offset,'count':len(rows)}:
                raise MemoryRequestRejectedError('document vector append unavailable')
        result=await write('document_vector_intent_finalize',original)
        if (set(result)!={'version','state','generation_id','publication_id','source_watermark','current'}
                or result.get('version')!=1 or result.get('state')!='published' or result.get('current') is not True
                or result.get('source_watermark')!=head['source_watermark']
                or type(result.get('generation_id')) is not str or re.fullmatch(r'gen_vec_[0-9a-f]{64}',result['generation_id']) is None
                or type(result.get('publication_id')) is not str or re.fullmatch(r'pub_vec_[0-9a-f]{64}',result['publication_id']) is None):
            raise MemoryRequestRejectedError('document vector publication unavailable')
        return result

    async def _host_document_vector_page(self, authority, *, after, expected_watermark, limit, deadline):
        """Read receipt-verified metadata under the original personal account admission."""
        from routes.personal_routes import _PersonalDocumentAuthority
        if (type(authority) is not _PersonalDocumentAuthority or authority.snapshot is not None
                or authority.current_account() is not True or self._scope(authority.owner).owner!=authority.owner or not self._host_bounded_write_available()
                or type(deadline) is not float or not math.isfinite(deadline) or type(limit) is not int or not 1<=limit<=64
                or (after is not None and (type(after) is not str or not after or len(after)>512))
                or (after is not None and expected_watermark is None)
                or (expected_watermark is not None and (type(expected_watermark) is not str or re.fullmatch(r'[0-9a-f]{64}',expected_watermark) is None))):
            raise MemoryRequestRejectedError('document vector snapshot unavailable')
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation
        process=transport.process;task=self._owner_task
        await _queued_owned_thread(prepared.assert_valid)
        request={'version':1,'owner':authority.owner,'account_id':authority.account,'after':after,'expected_watermark':expected_watermark,'limit':limit}
        result=await self._s07_owner_rpc(authority.owner,self._workspace_id,{'phase':'document_vector_snapshot','request':request},deadline,read_ticket={})
        await _queued_owned_thread(prepared.assert_valid)
        if (authority.current_account() is not True or self._guarded_preparation is not prepared or self._owned_transport is not transport
                or self._owned_generation is not generation or transport.process is not process or self._owner_task is not task or task.done()
                or asyncio.get_running_loop().time()>=deadline or not transport.current(generation) or type(result) is not dict
                or set(result)!={'version','state','source_watermark','total','items','next_cursor'}
                or type(result.get('version')) is not int or result['version']!=1 or result.get('state')!='available'
                or type(result.get('source_watermark')) is not str or re.fullmatch(r'[0-9a-f]{64}',result['source_watermark']) is None
                or (expected_watermark is not None and result['source_watermark']!=expected_watermark)
                or type(result.get('total')) is not int or not 0<=result['total']<=100000
                or type(result.get('items')) is not list or len(result['items'])>limit):
            raise MemoryRequestRejectedError('document vector snapshot unavailable')
        prior=after or ''
        for item in result['items']:
            if (type(item) is not dict or set(item)!={'source_id','source_uri','source_revision','document_id','chunk_id','content_hash'}
                    or type(item['source_id']) is not str or re.fullmatch(r'src_[0-9a-f]{32}',item['source_id']) is None
                    or type(item['document_id']) is not str or re.fullmatch(r'doc_[0-9a-f]{32}',item['document_id']) is None
                    or type(item['chunk_id']) is not str or re.fullmatch(r'chunk_[0-9a-f]{32}',item['chunk_id']) is None or item['chunk_id']<=prior
                    or type(item['source_uri']) is not str or not item['source_uri'] or len(item['source_uri'].encode('utf-8'))>1024
                    or type(item['source_revision']) is not int or item['source_revision']<1
                    or type(item['content_hash']) is not str or re.fullmatch(r'[0-9a-f]{64}',item['content_hash']) is None):
                raise MemoryRequestRejectedError('document vector snapshot unavailable')
            prior=item['chunk_id']
        cursor=result['next_cursor']
        if ((after is None and result['total']>len(result['items']) and cursor is None)
                or (cursor is not None and (type(cursor) is not str or not result['items'] or cursor!=prior or len(result['items'])!=limit))):
            raise MemoryRequestRejectedError('document vector snapshot unavailable')
        return result

    async def _host_document_head(self, authority, *, deadline):
        """Read the exact current personal source head, never an issue verdict."""
        from routes.personal_routes import _PersonalDocumentAuthority
        from src.openclank.personal_document_source import _PersonalDocumentSnapshot
        from src.memery_database import GuardedStorePreparation
        from src.openclank.mimo_supervisor import _queued_owned_thread
        from src.openclank.fm_owned_stdio import OwnedStdio
        if (type(authority) is not _PersonalDocumentAuthority or type(authority.snapshot) is not _PersonalDocumentSnapshot
                or type(deadline) is not float or not math.isfinite(deadline) or authority.current_source() is not True):
            raise MemoryRequestRejectedError('personal document head unavailable')
        prepared=self._guarded_preparation; transport=self._owned_transport; generation=self._owned_generation; task=self._owner_task
        if (type(prepared) is not GuardedStorePreparation or (prepared.capabilities.get('document_fts') is not True or prepared.capabilities.get('document_import_v1') is not True)
                or type(transport) is not OwnedStdio or not transport.current(generation) or task is None or task.done()
                or self._scope(authority.owner).owner!=authority.owner):
            raise MemoryRequestRejectedError('personal document head unavailable')
        process=transport.process
        try: await _queued_owned_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('personal document head unavailable') from exc
        if authority.current_source() is not True: raise MemoryRequestRejectedError('personal document authority changed')
        request={'version':1,'account_id':authority.account,'current_owner':authority.owner,
            'captured_owner':authority.owner,'source_uri':str(authority.snapshot._path)}
        read_ticket={}
        result=await self._s07_owner_rpc(authority.owner,self._workspace_id,
            {'phase':'document_source_lookup','request':request},deadline,read_ticket=read_ticket)
        if not (result=={'version':1,'state':'absent'} and type(result.get('version')) is int):
            if (type(result) is not dict or set(result)!={'version','state','source_id','source_revision','content_sha256','forget_state'}
                or type(result.get('version')) is not int or result['version']!=1 or result.get('state')!='present'
                or type(result.get('source_id')) is not str or re.fullmatch(r'src_[0-9a-f]{32}',result['source_id']) is None
                or type(result.get('source_revision')) is not int or result['source_revision']<1
                or result.get('forget_state') not in ('active','forgotten')
                or type(result.get('content_sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}',result['content_sha256']) is None):
                raise MemoryRequestRejectedError('personal document head unavailable')
        try: await _queued_owned_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('personal document head unavailable') from exc
        if (authority.current_source() is not True or self._guarded_preparation is not prepared
                or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or task.done() or not transport.current(generation) or transport.process is not process
                or read_ticket.get('transport') is not transport or read_ticket.get('generation') is not generation):
            raise MemoryRequestRejectedError('personal document authority changed')
        return result

    async def _host_document_structure(self, *, owner, request, deadline, current):
        """One bounded owner frame for the scoped structural job."""
        from src.memery_database import GuardedStorePreparation
        from src.openclank.mimo_supervisor import _queued_owned_thread
        prepared = self._guarded_preparation
        if (type(prepared) is not GuardedStorePreparation or prepared.document_structure is not True
                or type(request) is not dict or request.get("action") not in ("advance","read","revoke")
                or not callable(current) or current() is not True or self._scope(owner).owner != owner
                or not isinstance(deadline,float) or asyncio.get_running_loop().time() >= deadline):
            return {"state":"unavailable"}
        await _queued_owned_thread(prepared.assert_valid)
        def issue_current():
            try:
                observed = os.stat(prepared.database_path,follow_symlinks=False)
                return (self._guarded_preparation is prepared and current() is True
                        and (observed.st_dev,observed.st_ino) == (prepared.device,prepared.inode))
            except Exception:
                return False
        if request["action"] == "read":
            ticket = {}
            result = await self._s07_owner_rpc(owner,self._workspace_id,
                {"phase":"document_structure_read","request":request},deadline,read_ticket=ticket)
            if ticket.get("transport") is not self._owned_transport or ticket.get("generation") is not self._owned_generation:
                return {"state":"unavailable"}
        else:
            result = await self._host_candidate_request(owner,self._workspace_id,
                {"phase":"document_structure_write","request":request},require_live=True,
                write_deadline=deadline,write_status={},before_issue=issue_current)
        if not issue_current() or not isinstance(result,dict):
            return {"state":"unavailable"}
        return result

    async def _host_document_segments(self, *, owner, texts, current):
        if (not callable(current) or current() is not True or self._scope(owner).owner != owner
                or type(texts) is not list or not 1 <= len(texts) <= 16 or any(type(text) is not str for text in texts)
                or sum(len(text.encode()) for text in texts) > 256 * 1024):
            raise MemoryRequestRejectedError('document structure unavailable')
        deadline = asyncio.get_running_loop().time() + 5
        ticket = {}
        result = await self._s07_owner_rpc(owner, self._workspace_id, {'phase': 'document_segments', 'texts': texts},
                                           deadline, read_ticket=ticket)
        if (current() is not True or ticket.get('transport') is not self._owned_transport
                or ticket.get('generation') is not self._owned_generation
                or type(result) is not dict or set(result) != {'documents'}
                or type(result['documents']) is not list or len(result['documents']) != len(texts)):
            raise MemoryRequestRejectedError('document structure unavailable')
        return result['documents']

    async def _host_document_upsert(self, authority, *, operation_id, expected_head, deadline, write_status):
        """Issue one private personal-document operation under its actual Host owner."""
        from routes.personal_routes import _PersonalDocumentAuthority
        from src.openclank.personal_document_source import _PersonalDocumentSnapshot
        if (type(authority) is not _PersonalDocumentAuthority or type(authority.snapshot) is not _PersonalDocumentSnapshot
                or type(operation_id) is not str or not operation_id or operation_id != operation_id.strip()
                or len(operation_id.encode('utf-8')) > 1024 or type(expected_head) is not dict
                or type(write_status) is not dict or 'ticket' in write_status
                or type(deadline) is not float or not math.isfinite(deadline)
                or authority.current_source() is not True or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('personal document authority unavailable')
        from src.memery_database import GuardedStorePreparation
        from src.openclank.mimo_supervisor import _queued_owned_thread
        prepared=self._guarded_preparation
        if type(prepared) is not GuardedStorePreparation or prepared.capabilities.get('document_fts') is not True or prepared.capabilities.get('document_import_v1') is not True:
            raise MemoryRequestRejectedError('personal document profile unavailable')
        try: await _queued_owned_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('personal document profile unavailable') from exc
        if not (expected_head == {'kind':'absent'} or (
                set(expected_head)=={'kind','source_id','revision','content_sha256','forget_state'}
                and expected_head.get('kind')=='revision' and type(expected_head.get('source_id')) is str
                and re.fullmatch(r'src_[0-9a-f]{32}',expected_head['source_id']) is not None
                and type(expected_head.get('revision')) is int and expected_head['revision']>0
                and type(expected_head.get('content_sha256')) is str
                and re.fullmatch(r'[0-9a-f]{64}',expected_head['content_sha256']) is not None
                and expected_head.get('forget_state') in ('active','forgotten'))):
            raise MemoryRequestRejectedError('personal document head unavailable')
        snapshot=authority.snapshot; owner=authority.owner; workspace=self._workspace_id
        request={'version':2,'operation_id':operation_id,'account_id':authority.account,'owner':owner,
            'captured_owner':owner,'workspace_id':'','project_id':'','source_uri':str(snapshot._path),
            'expected_head':copy.deepcopy(expected_head),'raw':{'byte_length':snapshot.raw_byte_length,'sha256':snapshot.raw_sha256},
            'extracted_text':snapshot.text,'extracted_sha256':hashlib.sha256(snapshot.text.encode('utf-8')).hexdigest(),
            'extractor_version':snapshot.extractor_version,'chunker_version':'frankenmemory-rag-v2'}
        if snapshot.page_ends:
            request['page_ends'] = list(snapshot.page_ends)
        try:
            raw=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
            digest=hashlib.sha256(b'openclank.document-upsert.request.v1\n'+raw).hexdigest()
        except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
            raise MemoryRequestRejectedError('personal document request unavailable') from exc
        if self._scope(owner).owner != owner or authority.current_source() is not True:
            raise MemoryRequestRejectedError('personal document authority changed')
        payload={'phase':'document_upsert','request':request}
        def current_issue():
            try:
                if self._guarded_preparation is not prepared or authority.current_source() is not True:
                    return False
                stat_result=os.stat(prepared.database_path,follow_symlinks=False)
                return (prepared.capabilities.get('document_fts') is True and prepared.capabilities.get('document_import_v1') is True and self._guarded_preparation is prepared
                    and (stat_result.st_dev,stat_result.st_ino)==(prepared.device,prepared.inode))
            except Exception:
                return False
        result=await self._host_candidate_request(owner,workspace,payload,require_live=True,write_deadline=deadline,
            write_status=write_status,before_issue=current_issue)
        self._validate_document_original(result,operation_id,digest)
        if result.get('state') != 'committed' or 'chunk_count' not in result:
            raise MemoryRequestRejectedError('personal document outcome requires original status')
        if authority.current_account() is not True:
            raise MemoryRequestRejectedError('personal document authority changed')
        return result

    @staticmethod
    def _validate_document_original(result, operation_id, digest):
        if result == {'version':1,'state':'unknown'} and type(result.get('version')) is int:
            return
        keys={'version','state','operation_id','request_sha256','source_id','source_revision','document_id','generation_id','publication_id'}
        if (type(result) is not dict or set(result) not in (keys, keys | {'chunk_count'}) or type(result.get('version')) is not int
                or ('chunk_count' in result and (type(result['chunk_count']) is not int or not 1 <= result['chunk_count'] <= 512))
                or result['version']!=1 or result.get('state')!='committed' or result.get('operation_id')!=operation_id
                or result.get('request_sha256')!=digest or type(result.get('source_revision')) is not int or result['source_revision']<1
                or any(type(result.get(k)) is not str or not result[k] or len(result[k].encode('utf-8'))>1024
                       for k in ('source_id','document_id','generation_id','publication_id'))):
            raise MemoryRequestRejectedError('personal document receipt unavailable')

    async def _host_document_remove(self, authority, *, operation_id, source_id, expected_head, deadline, write_status):
        """Issue one exact private personal-document removal under its source owner."""
        from routes.personal_routes import _PersonalDocumentAuthority
        from src.openclank.personal_document_source import _PersonalDocumentSnapshot
        from src.memery_database import GuardedStorePreparation
        from src.openclank.mimo_supervisor import _queued_owned_thread
        if (type(authority) is not _PersonalDocumentAuthority or type(authority.snapshot) is not _PersonalDocumentSnapshot
                or type(operation_id) is not str or not operation_id or operation_id!=operation_id.strip() or len(operation_id.encode('utf-8'))>1024
                or type(source_id) is not str or re.fullmatch(r'src_[0-9a-f]{32}',source_id) is None
                or type(expected_head) is not dict or set(expected_head)!={'revision','content_sha256'}
                or type(expected_head.get('revision')) is not int or expected_head['revision']<1
                or type(expected_head.get('content_sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}',expected_head['content_sha256']) is None
                or type(write_status) is not dict or 'ticket' in write_status or type(deadline) is not float or not math.isfinite(deadline)
                or authority.current_source() is not True or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('personal document removal unavailable')
        prepared=self._guarded_preparation
        if type(prepared) is not GuardedStorePreparation or prepared.capabilities.get('document_fts') is not True or prepared.capabilities.get('document_import_v1') is not True:
            raise MemoryRequestRejectedError('personal document profile unavailable')
        try: await _queued_owned_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('personal document profile unavailable') from exc
        owner=authority.owner
        request={'version':1,'operation_id':operation_id,'account_id':authority.account,'owner':owner,'captured_owner':owner,
            'source_id':source_id,'source_uri':str(authority.snapshot._path),'expected_head':copy.deepcopy(expected_head)}
        try:
            raw=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
            digest=hashlib.sha256(b'openclank.document-remove.request.v1\n'+raw).hexdigest()
        except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
            raise MemoryRequestRejectedError('personal document removal unavailable') from exc
        if self._scope(owner).owner!=owner or authority.current_source() is not True:
            raise MemoryRequestRejectedError('personal document authority changed')
        def current_issue():
            try:
                if self._guarded_preparation is not prepared or authority.current_source() is not True: return False
                stat_result=os.stat(prepared.database_path,follow_symlinks=False)
                return (prepared.capabilities.get('document_fts') is True and prepared.capabilities.get('document_import_v1') is True and self._guarded_preparation is prepared
                    and (stat_result.st_dev,stat_result.st_ino)==(prepared.device,prepared.inode))
            except Exception: return False
        result=await self._host_candidate_request(owner,self._workspace_id,{'phase':'document_remove','request':request},
            require_live=True,write_deadline=deadline,write_status=write_status,before_issue=current_issue)
        self._validate_document_removal(result,operation_id,digest)
        if result.get('state')!='removed': raise MemoryRequestRejectedError('personal document outcome requires original status')
        if result['source_id']!=source_id or result['source_revision']!=expected_head['revision']:
            raise MemoryRequestRejectedError('personal document removal receipt changed')
        if authority.current_account() is not True: raise MemoryRequestRejectedError('personal document authority changed')
        return result

    @staticmethod
    def _validate_document_removal(result, operation_id, digest):
        if result=={'version':1,'state':'unknown'} and type(result.get('version')) is int: return
        keys={'version','state','operation_id','request_sha256','source_id','source_revision','documents_removed','chunks_removed'}
        if (type(result) is not dict or set(result)!=keys or type(result.get('version')) is not int or result['version']!=1
                or result.get('state')!='removed' or result.get('operation_id')!=operation_id or result.get('request_sha256')!=digest
                or type(result.get('source_id')) is not str or re.fullmatch(r'src_[0-9a-f]{32}',result['source_id']) is None
                or any(type(result.get(k)) is not int or result[k]<1 for k in ('source_revision','documents_removed','chunks_removed'))):
            raise MemoryRequestRejectedError('personal document removal receipt unavailable')

    async def _host_document_original_fact(self, authority, *, selector, deadline, _removal=False):
        """Read a retained exact selector; this never settles or authorizes a write."""
        from routes.personal_routes import _PersonalDocumentAuthority
        from src.memery_database import GuardedStorePreparation
        from src.openclank.mimo_supervisor import _queued_owned_thread
        from src.openclank.fm_owned_stdio import OwnedStdio
        keys={'version','operation_id','request_sha256','account_id','current_owner','captured_owner'}
        if type(_removal) is not bool: raise MemoryRequestRejectedError('personal document original unavailable')
        if (type(authority) is not _PersonalDocumentAuthority or type(selector) is not dict or set(selector)!=keys
                or type(selector.get('version')) is not int or selector['version']!=1
                or selector.get('account_id')!=authority.account or selector.get('current_owner')!=authority.owner
                or any(type(selector.get(k)) is not str or not selector[k] or selector[k]!=selector[k].strip()
                       or len(selector[k].encode('utf-8'))>1024 for k in ('operation_id','account_id','current_owner','captured_owner'))
                or type(selector.get('request_sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}',selector['request_sha256']) is None
                or type(deadline) is not float or not math.isfinite(deadline) or authority.current_account() is not True):
            raise MemoryRequestRejectedError('personal document original unavailable')
        frozen=copy.deepcopy(selector); prepared=self._guarded_preparation; transport=self._owned_transport
        generation=self._owned_generation; task=self._owner_task
        if (type(prepared) is not GuardedStorePreparation or type(transport) is not OwnedStdio
                or not transport.current(generation) or task is None or task.done() or self._scope(authority.owner).owner!=authority.owner):
            raise MemoryRequestRejectedError('personal document original unavailable')
        process=transport.process
        try: await _queued_owned_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('personal document original unavailable') from exc
        if authority.current_account() is not True: raise MemoryRequestRejectedError('personal document authority changed')
        read_ticket={}
        result=await self._s07_owner_rpc(authority.owner,self._workspace_id,
            {'phase':'document_remove_original' if _removal else 'document_upsert_original','request':frozen},deadline,read_ticket=read_ticket)
        (self._validate_document_removal if _removal else self._validate_document_original)(result,frozen['operation_id'],frozen['request_sha256'])
        try: await _queued_owned_thread(prepared.assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('personal document original unavailable') from exc
        if (authority.current_account() is not True or self._guarded_preparation is not prepared
                or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or task.done() or not transport.current(generation)
                or transport.process is not process or read_ticket.get('transport') is not transport
                or read_ticket.get('generation') is not generation):
            raise MemoryRequestRejectedError('personal document original custody changed')
        return result

    async def _host_document_upsert_original(self, authority, *, ticket, deadline):
        """Read the same original fact; missing never grants a new issue."""
        from routes.personal_routes import _PersonalDocumentAuthority
        if (type(authority) is not _PersonalDocumentAuthority or type(ticket) is not dict
                or type(deadline) is not float or not math.isfinite(deadline) or authority.current_account() is not True):
            raise MemoryRequestRejectedError('personal document original unavailable')
        payload=ticket.get('owned_original_payload'); original=payload.get('request') if type(payload) is dict else None
        if (type(payload) is not dict or payload.get('phase')!='document_upsert' or type(original) is not dict
                or type(original.get('captured_owner')) is not str or original.get('account_id')!=authority.account):
            raise MemoryRequestRejectedError('personal document original unavailable')
        try:
            raw=json.dumps(original,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
            digest=hashlib.sha256(b'openclank.document-upsert.request.v1\n'+raw).hexdigest()
        except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
            raise MemoryRequestRejectedError('personal document original unavailable') from exc
        selector={'version':1,'operation_id':original['operation_id'],'request_sha256':digest,
            'account_id':authority.account,'current_owner':authority.owner,'captured_owner':original['captured_owner']}
        snapshot=await self._owned_original_checked_snapshot(ticket,'document_upsert',original)
        if authority.current_account() is not True: raise MemoryRequestRejectedError('personal document authority changed')
        read_ticket={}
        result=await self._s07_owner_rpc(authority.owner,self._workspace_id,
            {'phase':'document_upsert_original','request':selector},deadline,read_ticket=read_ticket)
        self._validate_document_original(result,selector['operation_id'],digest)
        await self._owned_original_checked_current(ticket,'document_upsert',original,snapshot)
        if (authority.current_account() is not True or read_ticket.get('transport') is not snapshot[1]
                or read_ticket.get('generation') is not snapshot[2]):
            raise MemoryRequestRejectedError('personal document authority changed')
        return result

    async def _host_document_remove_original(self, authority, *, ticket, deadline):
        """Reconcile one exact removal ticket; unknown never permits unlink."""
        from routes.personal_routes import _PersonalDocumentAuthority
        if (type(authority) is not _PersonalDocumentAuthority or type(ticket) is not dict
                or type(deadline) is not float or not math.isfinite(deadline) or authority.current_account() is not True):
            raise MemoryRequestRejectedError('personal document original unavailable')
        payload=ticket.get('owned_original_payload'); original=payload.get('request') if type(payload) is dict else None
        if (type(payload) is not dict or payload.get('phase')!='document_remove' or type(original) is not dict
                or type(original.get('captured_owner')) is not str or original.get('account_id')!=authority.account):
            raise MemoryRequestRejectedError('personal document original unavailable')
        try:
            raw=json.dumps(original,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
            digest=hashlib.sha256(b'openclank.document-remove.request.v1\n'+raw).hexdigest()
        except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
            raise MemoryRequestRejectedError('personal document original unavailable') from exc
        selector={'version':1,'operation_id':original['operation_id'],'request_sha256':digest,
            'account_id':authority.account,'current_owner':authority.owner,'captured_owner':original['captured_owner']}
        snapshot=await self._owned_original_checked_snapshot(ticket,'document_remove',original)
        if authority.current_account() is not True: raise MemoryRequestRejectedError('personal document authority changed')
        read_ticket={}
        result=await self._s07_owner_rpc(authority.owner,self._workspace_id,
            {'phase':'document_remove_original','request':selector},deadline,read_ticket=read_ticket)
        self._validate_document_removal(result,selector['operation_id'],digest)
        if result.get('state')=='removed' and (result['source_id']!=original['source_id'] or result['source_revision']!=original['expected_head']['revision']):
            raise MemoryRequestRejectedError('personal document removal receipt changed')
        await self._owned_original_checked_current(ticket,'document_remove',original,snapshot)
        if (authority.current_account() is not True or read_ticket.get('transport') is not snapshot[1]
                or read_ticket.get('generation') is not snapshot[2]):
            raise MemoryRequestRejectedError('personal document authority changed')
        return result

    def _owned_original_snapshot(self, ticket, phase, original, *, output_kind=None):
        """Prove this original owner I/O drained and pin the current read lifetime."""
        if (type(ticket) is not dict or type(original) is not dict or type(phase) is not str
            or ticket.get('owned_original_payload')!=({'phase':phase,'request':original} if output_kind is None else {'phase':phase,'request':original,'output_kind':output_kind})
            or (ticket.get('frame_digest') is not None and ticket.get('frame_digest')!=ticket.get('owned_original_frame_digest'))):
            raise MemoryRequestRejectedError('owned original identity unavailable')
        prepared=self._guarded_preparation;current=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        process=current.process if current is not None else None
        if (prepared is None or process is None or ticket.get('owned_original_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode)
            or current is None or not current.current(generation) or task is None or task.done()):
            raise MemoryRequestRejectedError('owned original custody unavailable')
        old=ticket.get('transport');old_process=ticket.get('owned_original_process');drained=ticket.get('owned_io_drained') is True
        if not drained and old is not None and old_process is not None and old.process is old_process and old.generation is ticket.get('generation') and old_process.returncode is not None:
            try:
                exited=old.exit_confirmation(ticket['generation'])
                drained=exited.done() and not exited.cancelled() and exited.result()==old_process.returncode
            except (RuntimeError,asyncio.CancelledError):drained=False
        if not drained: raise MemoryRequestRejectedError('owned original I/O unsettled')
        try:
            st=os.stat(prepared.database_path,follow_symlinks=False)
            if (st.st_dev,st.st_ino)!=(prepared.device,prepared.inode): raise OSError('database identity changed')
        except OSError as exc: raise MemoryRequestRejectedError('owned original custody unavailable') from exc
        return (prepared,current,generation,process,task)

    def _owned_original_snapshot_current(self, ticket, phase, original, snapshot, *, output_kind=None):
        latest=self._owned_original_snapshot(ticket,phase,original,output_kind=output_kind)
        if type(snapshot) is not tuple or len(snapshot)!=5 or any(a is not b for a,b in zip(latest,snapshot)):
            raise MemoryRequestRejectedError('owned original custody changed')
        return True

    async def _owned_original_checked_snapshot(self, ticket, phase, original, *, output_kind=None):
        from src.openclank.mimo_supervisor import _queued_owned_thread
        snapshot=self._owned_original_snapshot(ticket,phase,original,output_kind=output_kind)
        try: await _queued_owned_thread(snapshot[0].assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('owned original custody unavailable') from exc
        self._owned_original_snapshot_current(ticket,phase,original,snapshot,output_kind=output_kind)
        return snapshot

    async def _owned_original_checked_current(self, ticket, phase, original, snapshot, *, output_kind=None):
        from src.openclank.mimo_supervisor import _queued_owned_thread
        self._owned_original_snapshot_current(ticket,phase,original,snapshot,output_kind=output_kind)
        try: await _queued_owned_thread(snapshot[0].assert_valid)
        except (OSError,ValueError,sqlite3.Error) as exc: raise MemoryRequestRejectedError('owned original custody unavailable') from exc
        return self._owned_original_snapshot_current(ticket,phase,original,snapshot,output_kind=output_kind)

    def _host_s08_pending_policy(self, lifecycle):
        """Return only this account's retained in-process original, never a new request."""
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
        session=getattr(self,'_s08_policy_session',None)
        if (type(lifecycle) is not CompletedTurnLifecycleOwner or lifecycle.provider is not self
            or type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or lifecycle.account_id!=session.lifecycle.account_id): return None
        return session

    async def _host_s08_reconcile_policy(self, session, *, lifecycle, policy_projection, current):
        """Read the exact retained original under current named read authority; never send work."""
        if (type(session) is not _S08PolicySession or session._seal is not _S08_POLICY_SESSION_SEAL
            or session.provider is not self or getattr(self,'_s08_policy_session',None) is not session
            or lifecycle.provider is not self or lifecycle.account_id!=session.lifecycle.account_id
            or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('S08 original unavailable')
        if session.state=='new' and session.original is None:
            self._s08_policy_session=None; session.state='retired'; return {'version':1,'state':'not_issued'}
        kinds={'prepare_issued':'prepare','reserve_issued':'reserve','dispatch_issued':'attempt','commit_issued':'result','terminal_issued':'terminal','prepared_cancel_issued':'prepared_cancel'}
        kind=kinds.get(session.state)
        original=getattr(session,'phase_original',None)
        if kind is None or type(original) is not dict: return {'version':1,'state':'unknown'}
        phases={'prepare':'s08_model_prepare','reserve':'s08_model_reserve','attempt':'s08_model_dispatch','result':'s08_policy_commit','terminal':'s08_policy_terminal','prepared_cancel':'s08_policy_prepared_cancel'}
        ticket=session.ticket.get('ticket') if type(session.ticket) is dict else None
        snapshot=await self._owned_original_checked_snapshot(ticket,phases[kind],original)
        target=lifecycle.target_authority.resolve(lifecycle.account_id)
        result=await self._host_s08_original_scoped(owner=target.memory_owner,account_id=lifecycle.account_id,
            workspace_id=target.memory_workspace,policy_user=lifecycle.owner,policy_projection=policy_projection,
            kind=kind,original=original,current=current)
        if current() is not True or self._s08_policy_session is not session: raise MemoryRequestRejectedError('S08 original authority changed')
        await self._owned_original_checked_current(ticket,phases[kind],original,snapshot)
        if current() is not True or self._s08_policy_session is not session:
            raise MemoryRequestRejectedError('S08 original authority changed')
        # Preserve the exact queued intent for content-free cancellation, never reissue it.
        if kind=='prepare' and type(result) is dict and set(result)=={'version','state','job_id'} and type(result['version']) is int and result['version']==1 and result['state']=='recorded' and type(result['job_id']) is str and result['job_id']:
            session.prepared_job_id=result['job_id'];session.state='prepare_confirmed'
        # A dispatch response lost before arm still owns the same unarmed handle.
        if kind=='attempt' and type(result) is dict and set(result)=={'version','state','job_state','attempt_state','reservation_state'} and type(result['version']) is int and result['version']==1 and result['state']=='recorded':
            if (result['job_state'],result['attempt_state'],result['reservation_state']) in (('active','leased','reserved'),('active','running','dispatched')):
                session.state='dispatch_confirmed_unsent'
        # A recovered reserve is still the exact original and never authorizes a model send.
        if kind=='reserve' and type(result) is dict and set(result)=={'version','state','job_id','attempt_id','lease_epoch','foreground_epoch','reserve_receipt_id','request_sha256'} and type(result['version']) is int and result['version']==1 and result['state']=='recorded':
            if (result['job_id']!=original['reservation']['job_id'] or type(result['attempt_id']) is not str or not result['attempt_id']
                or type(result['lease_epoch']) is not int or result['lease_epoch']<1 or type(result['foreground_epoch']) is not int
                or type(result['reserve_receipt_id']) is not str or re.fullmatch(r'cs_s08modelreserve_[0-9a-f]{64}',result['reserve_receipt_id']) is None
                or type(result['request_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['request_sha256']) is None
                or session.handle is not None or getattr(session,'mode',None) not in ('rules','model','hybrid')):
                raise MemoryRequestRejectedError('S08 reservation original changed')
            session.reserve_receipt_id=result['reserve_receipt_id'];session.reserve_request_sha256=result['request_sha256'];session.foreground_epoch=result['foreground_epoch']
            session.attempt={'version':1,'original':copy.deepcopy(original['original']),'reservation_original':copy.deepcopy(original),'job_id':result['job_id'],'attempt_id':result['attempt_id'],'lease_epoch':result['lease_epoch']}
            session.state='reserve_confirmed_unsent'
        # Only an exact settled original plus the drained original I/O can retire custody.
        if kind=='result' and type(result) is dict and set(result)=={'version','state','candidate_id'} and type(result['version']) is int and result['version']==1:
            valid=((result['state']=='no_proposal' and result['candidate_id'] is None)
                or (result['state']=='staged' and type(result['candidate_id']) is str and bool(result['candidate_id'])))
            if valid:
                session.state='settled';session.result=copy.deepcopy(result);self._s08_policy_session=None
        elif kind=='terminal' and type(result) is dict and result=={'version':1,'state':'settled','disposition':original.get('disposition')} and type(result.get('version')) is int:
            session.state='settled';session.result=copy.deepcopy(result);self._s08_policy_session=None
        elif kind=='prepared_cancel' and type(result) is dict and type(result.get('version')) is int and (result=={'version':1,'state':'cancelled','job_id':getattr(session,'prepared_job_id',None)} or result=={'version':1,'state':'erased'}):
            # Erasure is a verified terminal fact for this exact original, never a grant to reissue.
            session.state='settled';session.result=copy.deepcopy(result);self._s08_policy_session=None
        return result

    async def _host_s08_last_retained_run(self, request, *, current):
        """Content-free retained settlement evidence, never an absolute last-run claim."""
        extra={'output_kind'} if type(request) is dict and 'output_kind' in request else set()
        body=self._native_ops_flat_scope(request,extra)
        output=body.get('output_kind','summary')
        if output not in ('summary','journal_synthesis','connection') or not callable(current) or not self._host_s08_policy_ready(output):
            return {'version':1,'state':'unavailable'}
        try:
            if current() is not True:return {'version':1,'state':'unavailable'}
            result=await self._s07_owner_rpc(body['owner'],body['workspace_id'],{'phase':'s08_last_retained_run','request':body},asyncio.get_running_loop().time()+1.5,read_ticket={})
            if current() is not True or not self._host_s08_policy_ready(output) or type(result) is not dict:return {'version':1,'state':'unavailable'}
            if result=={'version':1,'state':'unavailable'} and type(result.get('version')) is int:return result
            if result=={'version':1,'state':'empty','coverage':'retained_history'} and type(result.get('version')) is int:return result
            run=result.get('lastVerifiedRetainedRun')
            if (set(result)!={'version','state','coverage','lastVerifiedRetainedRun'} or type(result.get('version')) is not int or result['version']!=1
                    or result.get('state')!='available' or result.get('coverage')!='retained_history' or type(run) is not dict
                    or set(run)-{'tokens'}!={'settledAtMs','outcome'} or type(run.get('settledAtMs')) is not int or not 0<run['settledAtMs']<=9007199254740991
                    or run.get('outcome') not in ('staged','no_proposal','failed','cancelled')):
                return {'version':1,'state':'unavailable'}
            if 'tokens' in run:
                tokens=run['tokens']
                def valid_counts(counts):
                    return (type(counts) is dict and set(counts)=={'input','output'}
                            and all(type(n) is int and 0<=n<=10_000_000 for n in counts.values()))
                if (type(tokens) is not dict or set(tokens)!={'reserved','charged','supplierReported'}
                        or any(not valid_counts(tokens[key]) for key in ('reserved','charged'))
                        or any(tokens['charged'][key]>tokens['reserved'][key] for key in ('input','output'))
                        or (tokens['supplierReported'] is not None and
                            (run['outcome'] not in ('failed','cancelled') or not valid_counts(tokens['supplierReported'])
                             or tokens['supplierReported']!=tokens['charged']))):
                    run.pop('tokens')
            return result
        except Exception:
            return {'version':1,'state':'unavailable'}

    async def _host_negotiate_skill_draft_profile(self, *, lifecycle, barrier, admission):
        """Read one profile fact under the actual account admission; never enables dispatch."""
        from src.openclank.account_request_barrier import AccountRequestBarrier, _Admission
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
        if (type(lifecycle) is not CompletedTurnLifecycleOwner or lifecycle.provider is not self
                or type(barrier) is not AccountRequestBarrier or type(admission) is not _Admission
                or getattr(self, '_skill_draft_session', None) is not None
                or getattr(self, '_skill_draft_profile_negotiation', None) is not None):
            return False
        marker = object()
        self._skill_draft_profile_negotiation = marker
        self._skill_draft_profile_witness = None
        try:
            barrier.assert_live_admission(lifecycle.owner, admission)
            lifecycle.profile.assert_current()
            binding = lifecycle.target_authority.resolve(lifecycle.account_id)
            if (binding != lifecycle.binding or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)
                    or binding.memory_workspace != self._workspace_id or self._scope(binding.memory_owner).owner != binding.memory_owner
                    or not self._host_bounded_write_available()):
                return False
            transport, generation = self._owned_transport, self._owned_generation
            process, task, preparation = transport.process, self._owner_task, self._guarded_preparation
            if preparation is None or process is None or preparation is not lifecycle.profile.provider._guarded_preparation:
                return False
            await asyncio.to_thread(preparation.assert_valid)
            barrier.assert_live_admission(lifecycle.owner, admission)
            ticket = {}
            result = await self._s07_owner_rpc(binding.memory_owner, binding.memory_workspace,
                {'phase': 'skill_draft_capability', 'version': 1, 'account_id': lifecycle.account_id},
                float(asyncio.get_running_loop().time() + 2.0), read_ticket=ticket)
            expected = {'version': 1, 'state': 'ready', 'owner': binding.memory_owner,
                'workspace_id': binding.memory_workspace, 'account_id': lifecycle.account_id,
                'profile': 'openclank.s05.skill_draft.v1', 'purge_profile': 'openclank.s05.skill_purge.v1',
                'database_id': preparation.database_id, 'schema_cookie': preparation.schema_cookie}
            if (type(result) is not dict or result != expected or type(result.get('version')) is not int
                    or type(result.get('schema_cookie')) is not int):
                return False
            await asyncio.to_thread(preparation.assert_valid)
            barrier.assert_live_admission(lifecycle.owner, admission)
            lifecycle.profile.assert_current()
            if (self._skill_draft_profile_negotiation is not marker or getattr(self, '_skill_draft_session', None) is not None
                    or lifecycle.target_authority.resolve(lifecycle.account_id) != binding
                    or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)
                    or not self._host_bounded_write_available() or self._owned_transport is not transport
                    or self._owned_generation is not generation or transport.process is not process
                    or self._owner_task is not task or self._guarded_preparation is not preparation
                    or ticket.get('transport') is not transport or ticket.get('generation') is not generation
                    or ticket.get('issued') is not True or ticket.get('frame_seen') is not True
                    or ticket.get('frame_invalid') is not False):
                return False
            self._skill_draft_profile_witness = (transport, generation, process, task, preparation,
                preparation.schema_cookie, lifecycle, barrier, admission)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        finally:
            if getattr(self, '_skill_draft_profile_negotiation', None) is marker:
                self._skill_draft_profile_negotiation = None

    def _host_skill_draft_profile_current(self, lifecycle):
        """Recheck only the negotiated profile fact and its original account admission."""
        witness = getattr(self, '_skill_draft_profile_witness', None)
        if type(witness) is not tuple or len(witness) != 9 or witness[6] is not lifecycle:
            return False
        transport, generation, process, task, preparation, cookie, _, barrier, admission = witness
        def admission_current():
            loop = admission.complete.get_loop()
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                barrier.assert_live_admission(lifecycle.owner, admission)
                return True
            if running is not None or not loop.is_running():
                return False
            future = asyncio.run_coroutine_threadsafe(barrier.is_live(lifecycle.owner, admission), loop)
            try:
                return future.result(timeout=1.5) is True
            except Exception:
                future.cancel()
                return False
        try:
            if (not self._host_bounded_write_available() or self._owned_transport is not transport
                    or self._owned_generation is not generation or not transport.current(generation)
                    or transport.process is not process or self._owner_task is not task or task.done()
                    or self._guarded_preparation is not preparation or preparation.schema_cookie != cookie
                    or admission_current() is not True):
                return False
            preparation.assert_valid()
            lifecycle.profile.assert_current()
            if (lifecycle.target_authority.resolve(lifecycle.account_id) != lifecycle.binding
                    or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)):
                return False
            return admission_current() is True
        except Exception:
            return False

    def _host_s08_policy_ready(self, output='summary'):
        witness=self._s08_policy_witness
        return bool(output in ('summary','connection','journal_synthesis','importance_target') and type(witness) is tuple and len(witness)==7
            and self._host_bounded_write_available() and witness[0] is self._owned_transport
            and witness[1] is self._owned_generation and witness[2] is self._owned_transport.process
            and witness[3] is self._owner_task and witness[4] is self._guarded_preparation
            and witness[5]==self._guarded_preparation.schema_cookie and output in witness[6])

    async def _host_negotiate_s08_policy(self, *, owner, workspace_id):
        """Negotiate factual profile support on this exact owned lifetime."""
        self._s08_policy_witness=None
        if (not self._host_bounded_write_available() or self._scope(owner).owner!=owner
            or workspace_id!=self._workspace_id): return False
        transport=self._owned_transport;generation=self._owned_generation;process=transport.process
        task=self._owner_task;prepared=self._guarded_preparation
        try:
            if prepared is None or process is None: return False
            await asyncio.to_thread(prepared.assert_valid)
            cookie=prepared.schema_cookie
            if type(cookie) is not int or cookie<0: return False
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s08_policy_capability'},
                asyncio.get_running_loop().time()+1.5,read_ticket={})
            if (type(result) is not dict or set(result)!={'version','profile','modes','outputs'}
                or type(result['version']) is not int or result['version']!=1
                or result['profile']!='policy' or result['modes']!=['rules','model','hybrid']
                or result['outputs'] not in ([*base,*suffix] for base in ([],['summary'],['connection'],['summary','connection'],['summary','journal_synthesis'],['summary','journal_synthesis','connection']) for suffix in ([],['importance_target']))): return False
            await asyncio.to_thread(prepared.assert_valid)
            if (not self._host_bounded_write_available() or self._owned_transport is not transport
                or self._owned_generation is not generation or transport.process is not process
                or self._owner_task is not task or self._guarded_preparation is not prepared
                or prepared.schema_cookie!=cookie): return False
            self._s08_policy_witness=(transport,generation,process,task,prepared,cookie,tuple(result['outputs']))
            return True
        except Exception:
            self._s08_policy_witness=None
            return False

    async def _host_registered_connection_predicates(self, *, owner, account_id, workspace_id, policy_user, policy_projection, current):
        """Bounded registry facts under the actual current named-account guard."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(owner) is not str or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id
            or type(policy_user) is not str or not policy_user or type(account_id) is not str or not account_id or type(policy_projection) is not PolicyProjection
            or policy_projection._preparation is not self._guarded_preparation or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('connection registry unavailable')
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection:
            raise MemoryRequestRejectedError('connection registry unavailable')
        refs=coordinator.checked_refs(policy_user,account_id,workspace_id)
        if current() is not True: raise MemoryRequestRejectedError('connection registry authority changed')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'connection_predicate_options','account_id':account_id,'policy':refs},
            asyncio.get_running_loop().time()+1.5,read_ticket={})
        if (current() is not True or coordinator.projection is not policy_projection
            or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs):
            raise MemoryRequestRejectedError('connection registry authority changed')
        if (type(result) is not dict or set(result)!={'version','predicates'} or type(result['version']) is not int
            or result['version']!=1 or type(result['predicates']) is not list or len(result['predicates'])>128
            or any(type(v) is not dict or set(v)!={'predicate','version','sha256'}
                or type(v['predicate']) is not str or not v['predicate'] or len(v['predicate'])>512
                or type(v['version']) is not int or v['version']<1 or type(v['sha256']) is not str
                or re.fullmatch(r'[0-9a-f]{64}',v['sha256']) is None for v in result['predicates'])):
            raise MemoryRequestRejectedError('connection registry unavailable')
        return result

    async def _host_connection_review_manifest(self, *, owner, account_id, workspace_id, candidate_id, policy_user, policy_projection, current):
        """Read original contributor selectors under current named human authority."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(owner) is not str or self._scope(owner).owner != owner or workspace_id != self._workspace_id
            or type(account_id) is not str or not account_id or type(policy_user) is not str or not policy_user
            or type(candidate_id) is not str or not candidate_id or len(candidate_id)>512
            or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation
            or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('connection review unavailable')
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection:
            raise MemoryRequestRejectedError('connection review unavailable')
        refs=coordinator.checked_refs(policy_user,account_id,workspace_id)
        if current() is not True: raise MemoryRequestRejectedError('connection review authority changed')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'connection_review_manifest','account_id':account_id,'candidate_id':candidate_id,'policy':refs},
            asyncio.get_running_loop().time()+1.5,read_ticket={})
        if (current() is not True or coordinator.projection is not policy_projection
            or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs):
            raise MemoryRequestRejectedError('connection review authority changed')
        try:
            if (type(result) is not dict or set(result)!={'version','candidate_id','candidate_revision','candidate_sha256','contributors','selection'}
                or type(result['version']) is not int or result['version']!=1 or result['candidate_id']!=candidate_id
                or type(result['candidate_revision']) is not int or result['candidate_revision']!=3
                or type(result['candidate_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['candidate_sha256']) is None
                or type(result['contributors']) is not list or not 1<=len(result['contributors'])<=64
                or any(type(v) is not dict or set(v)!={'event_id','source'} or type(v['event_id']) is not str or not v['event_id'] or type(v['source']) is not dict for v in result['contributors'])
                or type(result['selection']) is not list or not 2<=len(result['selection'])<=10
                or any(type(v) is not dict or set(v)!={'block_id','revision'} or type(v['block_id']) is not str or not v['block_id'] or type(v['revision']) is not int or v['revision']<1 for v in result['selection'])):
                raise ValueError('invalid manifest')
            encoded=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(encoded)>262144: raise ValueError('oversized manifest')
            return json.loads(encoded)
        except (TypeError,ValueError,UnicodeError) as exc:
            raise MemoryRequestRejectedError('connection review unavailable') from exc

    async def _host_s08_review_kind(self, *, owner, account_id, workspace_id, candidate_id, policy_user, policy_projection, current):
        """Classify an exact S08 review candidate under current named authority."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(owner) is not str or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id
            or type(account_id) is not str or not account_id or type(policy_user) is not str or not policy_user
            or type(candidate_id) is not str or not candidate_id or len(candidate_id)>512
            or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation
            or not callable(current) or current() is not True): raise MemoryRequestRejectedError('S08 review unavailable')
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection: raise MemoryRequestRejectedError('S08 review unavailable')
        refs=coordinator.checked_refs(policy_user,account_id,workspace_id)
        if current() is not True: raise MemoryRequestRejectedError('S08 review authority changed')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s08_review_kind','account_id':account_id,'candidate_id':candidate_id,'policy':refs},asyncio.get_running_loop().time()+1.5,read_ticket={})
        if (current() is not True or coordinator.projection is not policy_projection or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs): raise MemoryRequestRejectedError('S08 review authority changed')
        if (type(result) is not dict or set(result)!={'version','candidate_id','kind'} or type(result['version']) is not int or result['version']!=1 or result['candidate_id']!=candidate_id or result['kind'] not in ('legacy_rules','policy_summary','connection','journal_synthesis')): raise MemoryRequestRejectedError('S08 review unavailable')
        return result

    async def _host_s08_policy_review_manifest(self, *, owner, account_id, workspace_id, candidate_id, output_kind, policy_user, policy_projection, current):
        """Read the exact review contributors under named current authority."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(owner) is not str or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id or output_kind not in ('summary','connection','journal_synthesis')
            or type(account_id) is not str or not account_id or type(policy_user) is not str or not policy_user or type(candidate_id) is not str or not candidate_id or len(candidate_id)>512
            or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('S08 review unavailable')
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection: raise MemoryRequestRejectedError('S08 review unavailable')
        refs=coordinator.checked_refs(policy_user,account_id,workspace_id)
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s08_policy_review_manifest','account_id':account_id,'candidate_id':candidate_id,'output_kind':output_kind,'policy':refs},asyncio.get_running_loop().time()+1.5,read_ticket={})
        if current() is not True or coordinator.projection is not policy_projection or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs: raise MemoryRequestRejectedError('S08 review authority changed')
        try:
            if output_kind=='journal_synthesis':
                # Preview of a journal proposal: the proposed text, the cited archive selectors (no source text), the producer (model route), the digest the apply echoes.
                journal=result.get('journal') if type(result) is dict else None;producer=result.get('producer') if type(result) is dict else None
                if (type(result) is not dict or set(result)!={'version','candidate_id','candidate_revision','candidate_sha256','output_kind','content','journal','producer'}
                    or type(result['version']) is not int or result['version']!=1 or result['candidate_id']!=candidate_id or type(result['candidate_revision']) is not int or result['candidate_revision']!=3
                    or type(result['candidate_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['candidate_sha256']) is None or result['output_kind']!='journal_synthesis'
                    or type(result['content']) is not str or not 1<=len(result['content'].encode('utf-8'))<=4096
                    or type(journal) is not dict or set(journal)!={'chat_id','selection_sha256','page_sha256','prior_cursor','end_seq','high_seq','cited'}
                    or type(journal['chat_id']) is not str or any(re.fullmatch(r'[0-9a-f]{64}',journal[k]) is None for k in ('selection_sha256','page_sha256') if type(journal[k]) is str) or any(type(journal[k]) is not str for k in ('selection_sha256','page_sha256'))
                    or any(type(journal[k]) is not int for k in ('prior_cursor','end_seq','high_seq')) or type(journal['cited']) is not list or not 1<=len(journal['cited'])<=32
                    or any(type(v) is not dict or set(v)!={'seq','actor_id','message_id','part_id','revision','content_hash','selector'} or type(v['selector']) is not dict for v in journal['cited'])
                    or type(producer) is not dict or set(producer)!={'actor','job_id','attempt_id','mode','model_route_id','assignment_sha256'} or any(type(v) is not str or not v for v in producer.values())): raise ValueError('invalid manifest')
            elif (type(result) is not dict or set(result)!={'version','candidate_id','candidate_revision','candidate_sha256','contributors','selection'} or type(result['version']) is not int or result['version']!=1 or result['candidate_id']!=candidate_id or type(result['candidate_revision']) is not int or result['candidate_revision']!=3 or type(result['candidate_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['candidate_sha256']) is None or type(result['contributors']) is not list or not 1<=len(result['contributors'])<=64 or any(type(v) is not dict or set(v)!={'event_id','source'} or type(v['event_id']) is not str or not v['event_id'] or type(v['source']) is not dict for v in result['contributors']) or type(result['selection']) is not list or not 2<=len(result['selection'])<=10 or any(type(v) is not dict or set(v)!={'block_id','revision'} or type(v['block_id']) is not str or not v['block_id'] or type(v['revision']) is not int or v['revision']<1 for v in result['selection'])): raise ValueError('invalid manifest')
            raw=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(raw)>262144: raise ValueError('manifest too large')
            return json.loads(raw)
        except (TypeError,ValueError,UnicodeError) as exc: raise MemoryRequestRejectedError('S08 review unavailable') from exc

    async def _host_s08_policy_review_reject_preview(self, *, owner, account_id, workspace_id, candidate_id, output_kind, policy_user, policy_projection, current):
        """Read an exact reject selector without hydrating source content."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(owner) is not str or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id or output_kind not in ('summary','connection','journal_synthesis') or type(account_id) is not str or not account_id or type(policy_user) is not str or not policy_user or type(candidate_id) is not str or not candidate_id or len(candidate_id)>512 or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation or not callable(current) or current() is not True): raise MemoryRequestRejectedError('S08 review unavailable')
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection: raise MemoryRequestRejectedError('S08 review unavailable')
        refs=coordinator.checked_refs(policy_user,account_id,workspace_id)
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s08_policy_review_reject_preview','account_id':account_id,'candidate_id':candidate_id,'output_kind':output_kind,'policy':refs},asyncio.get_running_loop().time()+1.5,read_ticket={})
        if current() is not True or coordinator.projection is not policy_projection or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs: raise MemoryRequestRejectedError('S08 review authority changed')
        if type(result) is not dict or set(result)!={'version','candidate_id','candidate_revision','candidate_sha256'} or type(result['version']) is not int or result['version']!=1 or result['candidate_id']!=candidate_id or type(result['candidate_revision']) is not int or result['candidate_revision']!=3 or type(result['candidate_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',result['candidate_sha256']) is None: raise MemoryRequestRejectedError('S08 review unavailable')
        return result

    async def _host_s08_policy_review_original(self, *, owner, account_id, workspace_id, candidate_id, output_kind, policy_user, policy_projection, current, candidate_sha256=None):
        """Discover only the exact historical human review; no absence grants retry."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(owner) is not str or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id or output_kind not in ('summary','connection','journal_synthesis') or type(account_id) is not str or not account_id or type(policy_user) is not str or not policy_user or type(candidate_id) is not str or not candidate_id or len(candidate_id)>512 or (candidate_sha256 is not None and (type(candidate_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}',candidate_sha256) is None)) or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation or not callable(current) or current() is not True): raise MemoryRequestRejectedError('S08 review unavailable')
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection: raise MemoryRequestRejectedError('S08 review unavailable')
        refs=coordinator.checked_refs(policy_user,account_id,workspace_id);ticket=self._s08_policy_review_ticket;snapshot=None;original=None
        if ticket is not None:
            payload=ticket.get('owned_original_payload');original=payload.get('request') if type(payload) is dict else None
            if (type(original) is not dict or payload.get('output_kind')!=output_kind or original.get('account_id')!=account_id or original.get('workspace_id')!=workspace_id or original.get('candidate_id')!=candidate_id or (candidate_sha256 is not None and candidate_sha256!=original.get('candidate_sha256'))): raise MemoryRequestRejectedError('S08 original identity changed')
            candidate_sha256=original.get('candidate_sha256')
            def unknown_original():
                prepared=self._guarded_preparation
                if (self._s08_policy_review_ticket is not ticket or prepared is None or ticket.get('owned_original_database')!=(prepared.database_path,prepared.database_id,prepared.device,prepared.inode) or current() is not True or coordinator.projection is not policy_projection or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs): raise MemoryRequestRejectedError('S08 original custody changed')
                return {'state':'found','original':{k:original[k] for k in ('candidate_id','candidate_revision','candidate_sha256','request_key','reason','action')},'receipt':{'state':'unknown','candidate_id':candidate_id}}
            try: snapshot=await self._owned_original_checked_snapshot(ticket,'s08_policy_review_apply',original,output_kind=output_kind)
            except MemoryRequestRejectedError: return unknown_original()
        read_ticket={}
        try:
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s08_policy_review_original','account_id':account_id,'candidate_id':candidate_id,'candidate_sha256':candidate_sha256,'output_kind':output_kind,'policy':refs},asyncio.get_running_loop().time()+1.5,read_ticket=read_ticket)
        except (OSError,ConnectionError,TimeoutError,MemoryTransportError):
            if ticket is not None: return unknown_original()
            raise
        if current() is not True or coordinator.projection is not policy_projection or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs: raise MemoryRequestRejectedError('S08 review authority changed')
        if type(result) is not dict or result.get('state') not in ('absent','found','terminal_erased'):
            if ticket is not None: return unknown_original()
            raise MemoryRequestRejectedError('S08 original unavailable')
        if result['state']=='absent':
            if set(result)!={'state'}: raise MemoryRequestRejectedError('S08 original unavailable')
            return unknown_original() if ticket is not None else result
        if result['state']=='terminal_erased':
            if set(result)!={'state','candidate_id'} or result['candidate_id']!=candidate_id or candidate_sha256 is None: raise MemoryRequestRejectedError('S08 original unavailable')
        else:
            original_fact=result.get('original');receipt=result.get('receipt');key='block_id' if output_kind in ('summary','journal_synthesis') else 'relation_block_id'
            if (set(result)!={'state','original','receipt'} or type(original_fact) is not dict or set(original_fact)!={'candidate_id','candidate_revision','candidate_sha256','request_key','reason','action'} or original_fact['candidate_id']!=candidate_id or type(original_fact['candidate_revision']) is not int or original_fact['candidate_revision']!=3 or type(original_fact['candidate_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',original_fact['candidate_sha256']) is None or (candidate_sha256 is not None and original_fact['candidate_sha256']!=candidate_sha256) or type(original_fact['request_key']) is not str or not original_fact['request_key'] or type(original_fact['reason']) is not str or not original_fact['reason'] or original_fact['action'] not in ('accept','reject') or type(receipt) is not dict or receipt.get('candidate_id')!=candidate_id or receipt.get('visibility')!='not_rechecked' or (original_fact['action']=='accept' and (set(receipt)!={'state','candidate_id',key,'revision','visibility'} or receipt['state']!='applied' or type(receipt[key]) is not str or not receipt[key] or type(receipt['revision']) is not int or receipt['revision']!=1)) or (original_fact['action']=='reject' and (set(receipt)!={'state','candidate_id','visibility'} or receipt['state']!='rejected'))): raise MemoryRequestRejectedError('S08 original unavailable')
            if ticket is not None and any(original_fact[k]!=original[k] for k in ('candidate_id','candidate_revision','candidate_sha256','request_key','reason','action')): raise MemoryRequestRejectedError('S08 original identity changed')
        if ticket is not None:
            await self._owned_original_checked_current(ticket,'s08_policy_review_apply',original,snapshot,output_kind=output_kind)
            if (current() is not True or coordinator.projection is not policy_projection or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs or self._s08_policy_review_ticket is not ticket or read_ticket.get('transport') is not snapshot[1] or read_ticket.get('generation') is not snapshot[2]): raise MemoryRequestRejectedError('S08 original custody changed')
            self._s08_policy_review_ticket=None
        return result

    async def _host_s08_policy_review_apply(self, request, *, output_kind, before_issue):
        """Issue one current human review and retain its exact original ticket."""
        if type(request) is not dict or output_kind not in ('summary','connection','journal_synthesis') or not callable(before_issue) or before_issue() is not True or self._s08_policy_review_ticket is not None: raise MemoryRequestRejectedError('S08 review unavailable')
        try:
            raw=json.dumps(request,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(raw)>262144: raise ValueError('request too large')
            original=json.loads(raw)
        except (TypeError,ValueError,UnicodeError) as exc: raise MemoryRequestRejectedError('S08 review unavailable') from exc
        fields={'version','owner','account_id','workspace_id','candidate_id','candidate_revision','candidate_sha256','request_key','reason','action','policy_projection'}
        if set(original)!=fields or type(original['version']) is not int or original['version']!=1 or type(original['candidate_revision']) is not int or original['candidate_revision']!=3 or any(type(original[k]) is not str or not original[k] or len(original[k])>512 for k in ('owner','account_id','workspace_id','candidate_id','request_key','reason')) or type(original['candidate_sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',original['candidate_sha256']) is None or original['action'] not in ('accept','reject') or type(original['policy_projection']) is not dict or self._scope(original['owner']).owner!=original['owner'] or original['workspace_id']!=self._workspace_id: raise MemoryRequestRejectedError('S08 review unavailable')
        status={};result=await self._host_candidate_request(original['owner'],original['workspace_id'],{'phase':'s08_policy_review_apply','request':original,'output_kind':output_kind},require_live=True,write_deadline=float(asyncio.get_running_loop().time()+1.5),before_issue=before_issue,write_status=status)
        key='block_id' if output_kind in ('summary','journal_synthesis') else 'relation_block_id'
        if (type(result) is not dict or result.get('candidate_id')!=original['candidate_id'] or result.get('visibility') not in ('current','not_rechecked') or (original['action']=='accept' and (set(result)!={'state','candidate_id',key,'revision','visibility'} or result['state']!='applied' or type(result[key]) is not str or not result[key] or type(result['revision']) is not int or result['revision']!=1)) or (original['action']=='reject' and (set(result)!={'state','candidate_id','visibility'} or result['state']!='rejected')) or self._s08_policy_review_ticket is not status.get('ticket') or before_issue() is not True): raise MemoryRequestRejectedError('S08 review outcome unavailable')
        return result

    async def _host_s08_original_scoped(self, *, owner, account_id, workspace_id, policy_user, policy_projection, kind, original, current):
        """Read unchanged original S08 facts under separately held current authority."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(owner) is not str or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id
            or type(account_id) is not str or not account_id or type(policy_user) is not str or not policy_user
            or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation
            or kind not in ('prepare','reserve','attempt','result','terminal','prepared_cancel') or type(original) is not dict
            or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('S08 original unavailable')
        try:
            encoded=json.dumps(original,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(encoded)>262144: raise ValueError('original too large')
            frozen=json.loads(encoded)
        except (TypeError,ValueError,UnicodeError) as exc:
            raise MemoryRequestRejectedError('S08 original unavailable') from exc
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection: raise MemoryRequestRejectedError('S08 original unavailable')
        refs=coordinator.checked_refs(policy_user,account_id,workspace_id)
        if current() is not True: raise MemoryRequestRejectedError('S08 original authority changed')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s08_original_scoped','account_id':account_id,'policy':refs,'kind':kind,'request':frozen},
            asyncio.get_running_loop().time()+1.5,read_ticket={})
        if (current() is not True or coordinator.projection is not policy_projection
            or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs):
            raise MemoryRequestRejectedError('S08 original authority changed')
        if type(result) is not dict or type(result.get('version')) is not int or result['version']!=1 or type(result.get('state')) is not str:
            raise MemoryRequestRejectedError('S08 original unavailable')
        try:
            data=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(data)>262144: raise ValueError('result too large')
            return json.loads(data)
        except (TypeError,ValueError,UnicodeError) as exc:
            raise MemoryRequestRejectedError('S08 original unavailable') from exc

    async def _host_s08_policy_snapshots(self, *, owner, account_id, workspace_id, policy_user, selection, policy_projection, current):
        """Read factual S02 snapshots for an already-held S08 source selection."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(owner) is not str or self._scope(owner).owner!=owner or workspace_id!=self._workspace_id
            or type(policy_user) is not str or not policy_user or type(account_id) is not str or not account_id or type(selection) is not dict
            or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation
            or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('S08 policy unavailable')
        try:
            encoded=json.dumps(selection,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(encoded)>262144: raise ValueError('selection unavailable')
            frozen=json.loads(encoded)
        except (TypeError,ValueError,UnicodeError) as exc:
            raise MemoryRequestRejectedError('S08 policy unavailable') from exc
        target_profile='importance_target' in frozen
        if (frozen.get('owner')!=owner or frozen.get('workspace_id')!=workspace_id or frozen.get('source_account_id')!=account_id
            or type(frozen.get('candidates')) is not list or (len(frozen['candidates'])!=1 if target_profile else not 2<=len(frozen['candidates'])<=10)
            or (target_profile and type(frozen['importance_target']) is not dict)):
            raise MemoryRequestRejectedError('S08 policy scope unavailable')
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection:
            raise MemoryRequestRejectedError('S08 policy unavailable')
        refs=coordinator.checked_refs(policy_user,account_id,workspace_id)
        if current() is not True: raise MemoryRequestRejectedError('S08 authority changed')
        result=await self._host_candidate_request(owner,workspace_id,
            {'phase':'s08_policy_snapshots','selection':frozen,'policy':refs},require_live=True)
        if (current() is not True or coordinator.projection is not policy_projection
            or coordinator.checked_refs(policy_user,account_id,workspace_id)!=refs):
            raise MemoryRequestRejectedError('S08 authority changed')
        if (type(result) is not dict or set(result)!={'version','state','selection_sha256','source_sha256','commitment_sha256','snapshots'}
            or type(result['version']) is not int or result['version']!=1 or result['state']!='ready'
            or any(type(result[k]) is not str or re.fullmatch(r'[0-9a-f]{64}',result[k]) is None for k in ('selection_sha256','source_sha256','commitment_sha256'))
            or type(result['snapshots']) is not list or len(result['snapshots'])!=len(frozen['candidates'])
            or any(type(item) is not dict or set(item)!=({'reference','mirror_sha256','snapshot_sha256','typed_value','value_json','snapshot'} if target_profile else {'reference','mirror_sha256','snapshot_sha256','typed_value','snapshot'})
                or (target_profile and (type(item['value_json']) is not str or not 0<len(item['value_json'].encode('utf-8'))<=16384))
                or type(item['reference']) is not dict or type(item['typed_value']) is not dict or type(item['snapshot']) is not dict
                or any(type(item[k]) is not str or re.fullmatch(r'[0-9a-f]{64}',item[k]) is None for k in ('mirror_sha256','snapshot_sha256'))
                for item in result['snapshots'])):
            raise MemoryRequestRejectedError('S08 policy result unavailable')
        return result

    async def _host_s08_rules_once(self, *, lifecycle, selection, origins, policy_projection, assignment, guards, router):
        """One fixed local rules job; caller retains every source chat guard."""
        from src.memery_consolidation import ConsolidationSelection, bounded_codelivery_pairs, s08_queue_limits
        from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
        from src.openclank.memory_job_admission import AssignmentAuthority, _completed_turn_limits
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(lifecycle) is not CompletedTurnLifecycleOwner or lifecycle.provider is not self
                or type(selection) is not ConsolidationSelection or type(origins) is not tuple
                or type(assignment) is not AssignmentAuthority or assignment.owner != lifecycle.owner
                or type(policy_projection) is not PolicyProjection or policy_projection._preparation is not self._guarded_preparation
                or not 2 <= len(selection.memories) <= 10 or not 1 <= len(selection.evidence) <= 512
                or len(origins) != len(selection.evidence) or selection.turn_count < 3 or type(guards) is not dict):
            raise MemoryRequestRejectedError('S08 rules unavailable')
        lifecycle.profile.assert_current()
        binding = lifecycle.target_authority.resolve(lifecycle.account_id)
        if binding != lifecycle.binding or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner):
            raise MemoryRequestRejectedError('S08 owner unavailable')
        base, limits = _completed_turn_limits(lifecycle.target_authority, assignment)
        coordinator=policy_coordinator()
        if coordinator is None or coordinator.projection is not policy_projection:
            raise MemoryRequestRejectedError('S08 policy unavailable')
        refs=coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,binding.memory_workspace)
        admissions=[]
        for item, origin in bounded_codelivery_pairs(selection,origins):
            if (type(origin) is not _HostObservationProjection or origin.source != item.source
                    or origin.owner != binding.memory_owner or origin.workspace_id != binding.memory_workspace
                    or item.source.account_id != lifecycle.account_id):
                raise MemoryRequestRejectedError('S08 source unavailable')
            guard=guards.get(item.source.chat_id)
            if guard is None: raise MemoryRequestRejectedError('S08 source guard unavailable')
            await asyncio.to_thread(lifecycle.for_source,source=item.source,guard=guard)
            authority={key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
            admissions.append({'event_id':item.event_id,'source':asdict(item.source),'authority':authority})
        if _completed_turn_limits(lifecycle.target_authority,assignment)!=(base,limits):
            raise MemoryRequestRejectedError('S08 policy changed')
        admissions.sort(key=lambda item:item['event_id'])
        candidates=sorted((asdict(item) for item in selection.memories),key=lambda item:item['block_id'])
        material={'account':lifecycle.account_id,'target':binding.fingerprint,'policy':base['fingerprint'],
                  'candidates':candidates,'admissions':admissions}
        key='s08_'+hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        request={'selection':{'version':1,'owner':binding.memory_owner,'workspace_id':binding.memory_workspace,
            'project_id':None,'source_account_id':lifecycle.account_id,'idempotency_key':key,
            'target_fingerprint':binding.fingerprint,'policy_fingerprint':base['fingerprint'],
            **s08_queue_limits(limits),
            'candidates':candidates,'admissions':admissions},
            'worker_id':'s08_'+hashlib.sha256((lifecycle.account_id+'\0'+lifecycle.profile.coverage_generation+'\0'+lifecycle.profile.clock_generation).encode()).hexdigest(),
            'coverage_generation':lifecycle.profile.coverage_generation,'clock_generation':lifecycle.profile.clock_generation,
            'now_tick':lifecycle.profile.monotonic_tick(),'quiet_seconds':limits.quiet_seconds,
            'job_seconds':limits.job_seconds,'daily_seconds':limits.daily_seconds,
            'policy_projection':refs}
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        def current(opted_in):
            try:
                lifecycle.profile.assert_current()
                if (lifecycle.target_authority.resolve(lifecycle.account_id)!=binding
                    or lifecycle.account_lifecycle.owner_has_active_operation(lifecycle.owner)
                    or coordinator.projection is not policy_projection
                    or coordinator.checked_refs(lifecycle.owner,lifecycle.account_id,binding.memory_workspace)!=refs
                    or _completed_turn_limits(lifecycle.target_authority,assignment)!=(base,limits)):
                    return False
                with PREFERENCE_LOCK:
                    prefs=_load_for_user(lifecycle.owner)
                    if (prefs.get('memory_policy_consolidation_enabled') is True)!=opted_in: return False
                for item, _origin in bounded_codelivery_pairs(selection,origins):
                    lifecycle.for_source(source=item.source,guard=guards[item.source.chat_id])
                return True
            except Exception:
                return False
        with PREFERENCE_LOCK:
            opted_in=_load_for_user(lifecycle.owner).get('memory_policy_consolidation_enabled') is True
        if not current(opted_in): raise MemoryRequestRejectedError('S08 authority changed')
        if opted_in:
            from src.memery_consolidation import resolve_s08_native_evaluation
            snapshot=await self._host_s08_policy_snapshots(owner=binding.memory_owner,account_id=lifecycle.account_id,
                workspace_id=binding.memory_workspace,policy_user=lifecycle.owner,selection=request['selection'],policy_projection=policy_projection,
                current=lambda: current(True))
            mode,values=resolve_s08_native_evaluation(selection,snapshot)
            if mode is None or not current(True): raise MemoryRequestRejectedError('S08 policy deferred')
            # The effect owner must revalidate this exact decision and snapshot.
            # Until its same-attempt S05 ABI is present, never downgrade to rules.
            evaluated=(mode,hashlib.sha256(json.dumps({'selection':request['selection'],'policy':refs,
                'snapshot':snapshot,'assignment':base['fingerprint']},sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest(),values)
            if not evaluated[1]: raise MemoryRequestRejectedError('S08 policy unavailable')
            raise MemoryRequestRejectedError('S08 evaluated effect unavailable')
        result=await self._host_candidate_request(binding.memory_owner,binding.memory_workspace,
            {'phase':'s08_rules_once','owner':binding.memory_owner,'workspace_id':binding.memory_workspace,'request':request},
            require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),before_issue=lambda: current(False))
        if (type(result) is not dict or set(result)!={'state','job_id','candidate_id','replayed'}
                or result['state']!='succeeded' or type(result['replayed']) is not bool
                or type(result['job_id']) is not str or type(result['candidate_id']) is not str
                or not result['candidate_id'].startswith('s08candidate_')):
            raise MemoryRequestRejectedError('S08 result unavailable')
        return result

    async def _host_consolidation_admissions_page(self, origin: _HostObservationProjection, *, after: str | None = None, limit: int = 64):
        """Content-free evidence; caller holds fresh source and lifecycle guard."""
        import re
        from src.memery_consolidation import AdmittedEvidence
        from src.memery_external_sources import SourceRef, _source_ref_error
        from src.memory_provider import MemoryRevisionRef
        from src.openclank.memory_job_admission import _conn
        if (type(origin) is not _HostObservationProjection or type(origin.source) is not SourceRef
                or _provider_source_ref_error(self, origin.source) or origin.workspace_id != self._workspace_id
                or self._scope(origin.owner).owner != origin.owner or type(limit) is not int or not 1 <= limit <= 64
                or (after is not None and (type(after) is not str or not after or len(after)>512 or after!=after.strip()))):
            raise MemoryRequestRejectedError('consolidation evidence unavailable')
        try:
            with _conn(self._local_db_path()) as db:
                if not self._s06_source_capability(db) or not self._s06_admitted_capability(db):
                    raise ValueError('consolidation evidence unavailable')
        except (ValueError,TypeError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('consolidation evidence unavailable') from exc
        authority={key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
        result=await self._host_candidate_request(origin.owner,origin.workspace_id,
            {'phase':'consolidation_admissions_page','source':asdict(origin.source),'authority':authority,'after':after,'limit':limit},require_live=True)
        if type(result) is not dict or set(result)!={'events','next_after'} or type(result['events']) is not list or len(result['events'])>limit:
            raise MemoryRequestRejectedError('consolidation evidence unavailable')
        rows=[]
        for row in result['events']:
            if (type(row) is not dict or set(row)!={'event_id','signal','delivery_key','block_id','project_id','revision','value_sha256','source_account_id','chat_id','root_operation_id','occurred_at'}
                    or row['signal'] not in ('tool_admitted','context_admitted') or type(row['event_id']) is not str
                    or not re.fullmatch(r'cs_s06(?:admitted|context)_[0-9a-f]{64}',row['event_id'])
                    or (row['signal']=='tool_admitted') != row['event_id'].startswith('cs_s06admitted_')
                    or type(row['block_id']) is not str or not row['block_id'] or len(row['block_id'])>512
                    or (row['project_id'] is not None and (type(row['project_id']) is not str or not row['project_id']))
                    or type(row['delivery_key']) is not str or not re.fullmatch(r'[0-9a-f]{64}',row['delivery_key'])
                    or type(row['revision']) is not int or row['revision']<1 or type(row['value_sha256']) is not str
                    or not re.fullmatch(r'[0-9a-f]{64}',row['value_sha256']) or type(row['occurred_at']) is not str or len(row['occurred_at'])>64
                    or (row['source_account_id'],row['chat_id'],row['root_operation_id']) != (origin.source.account_id,origin.source.chat_id,origin.source.message_id)):
                raise MemoryRequestRejectedError('consolidation evidence unavailable')
            memory=MemoryRevisionRef(origin.owner,origin.workspace_id,row['project_id'],row['block_id'],row['revision'],row['value_sha256'])
            rows.append(AdmittedEvidence(row['event_id'],row['signal'],origin.source,memory,row['occurred_at'],row['delivery_key']))
        cursor=result['next_after']
        if cursor is not None and (type(cursor) is not str or len(cursor)>512 or not cursor.startswith('outbox_cs_s06') or (after is not None and cursor<=after)):
            raise MemoryRequestRejectedError('consolidation cursor unavailable')
        return tuple(rows),cursor

    def _host_helped_ready(self):
        witness=self._helped_witness
        return bool(type(witness) is tuple and len(witness)==5 and self._host_bounded_write_available()
            and witness[0] is self._owned_transport and witness[1] is self._owned_generation
            and witness[2] is self._owned_transport.process and witness[3] is self._owner_task
            and witness[4] is self._guarded_preparation)

    async def _host_negotiate_helped(self, *, owner, workspace_id):
        if self._host_helped_ready():
            try:
                await asyncio.to_thread(self._helped_witness[4].assert_valid)
                return self._host_helped_ready()
            except Exception:return False
        self._helped_witness=None
        if not self._host_bounded_write_available(): return False
        transport=self._owned_transport; generation=self._owned_generation; process=transport.process
        task=self._owner_task; prepared=self._guarded_preparation
        try:
            await asyncio.to_thread(prepared.assert_valid)
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'helped_capability'},asyncio.get_running_loop().time()+2.0)
            if (result!={'version':1,'state':'ready','phase':'helped_observation'} or type(result.get('version')) is not int
                    or not self._host_bounded_write_available() or self._owned_transport is not transport
                    or self._owned_generation is not generation or transport.process is not process
                    or self._owner_task is not task or self._guarded_preparation is not prepared): return False
            await asyncio.to_thread(prepared.assert_valid)
            self._helped_witness=(transport,generation,process,task,prepared)
            return self._host_helped_ready()
        except Exception:return False

    def _host_found_features_ready(self):
        witness=getattr(self,'_found_features_witness',None)
        return bool(type(witness) is tuple and len(witness)==5 and self._host_bounded_write_available()
            and witness[0] is self._owned_transport and witness[1] is self._owned_generation
            and witness[2] is self._owned_transport.process and witness[3] is self._owner_task
            and witness[4] is self._guarded_preparation)

    async def _host_negotiate_found_features(self, *, owner, workspace_id):
        # Mirrors _host_negotiate_helped. Never raises; any failure means "not available" and the Host omits the key.
        # The witness holds transport/generation/process/task/preparation identities, so a restarted or replaced Native is re-asked.
        if self._host_found_features_ready():
            try:
                await asyncio.to_thread(self._found_features_witness[4].assert_valid)
                return self._host_found_features_ready()
            except Exception:return False
        self._found_features_witness=None
        if not self._host_bounded_write_available(): return False
        transport=self._owned_transport; generation=self._owned_generation; process=transport.process
        task=self._owner_task; prepared=self._guarded_preparation
        try:
            await asyncio.to_thread(prepared.assert_valid)
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'found_features_capability'},asyncio.get_running_loop().time()+2.0)
            if (result!={'version':1,'feature':'found_features_v1','state':'ready'} or type(result.get('version')) is not int
                    or not self._host_bounded_write_available() or self._owned_transport is not transport
                    or self._owned_generation is not generation or transport.process is not process
                    or self._owner_task is not task or self._guarded_preparation is not prepared):
                logger.debug('found features capability unavailable'); return False
            await asyncio.to_thread(prepared.assert_valid)
            self._found_features_witness=(transport,generation,process,task,prepared)
            logger.debug('found features capability witnessed')
            return self._host_found_features_ready()
        except Exception:return False

    async def _host_found_features_page(self, origin, *, after=None, limit=64):
        """One source selector's found-feature rows: ids, numbers and the route label only."""
        import re
        from src.openclank.memory_context_admission import found_features_valid
        if (type(origin) is not _HostObservationProjection or not self._host_found_features_ready()
                or type(limit) is not int or not 1<=limit<=200 or (after is not None and (type(after) is not str or len(after)>512))):
            raise MemoryRequestRejectedError('found features unavailable')
        witness=self._found_features_witness
        from src.openclank.mimo_supervisor import _queued_owned_thread
        await _queued_owned_thread(witness[4].assert_valid)
        if self._found_features_witness is not witness or not self._host_found_features_ready(): raise MemoryRequestRejectedError('found features unavailable')
        authority={key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
        request={'version':1,'owner':origin.owner,'workspace_id':origin.workspace_id,'account_id':origin.source.account_id,
                 'source':asdict(origin.source),'authority':authority,'after':after,'limit':limit}
        result=await self._s07_owner_rpc(origin.owner,origin.workspace_id,{'phase':'found_features_page','request':request},asyncio.get_running_loop().time()+2.0)
        if (self._found_features_witness is not witness or not self._host_found_features_ready() or type(result) is not dict
                or set(result)!={'version','state','items','next_after'} or type(result['version']) is not int or result['version']!=1
                or result['state'] not in ('ready','partial') or type(result['items']) is not list or len(result['items'])>limit):
            raise MemoryRequestRejectedError('found features unavailable')
        seen=set()
        for item in result['items']:
            if (type(item) is not dict or set(item)!={'admitted_event_id','found_event_id','recall_attempt_id','features','recorded'}
                    or type(item['admitted_event_id']) is not str or not re.fullmatch(r'cs_s06(?:admitted|context)_[0-9a-f]{64}',item['admitted_event_id'])
                    or item['admitted_event_id'] in seen
                    or type(item['found_event_id']) is not str or not re.fullmatch(r'cs_s06found_[0-9a-f]{64}',item['found_event_id'])
                    or type(item['recall_attempt_id']) is not str or not item['recall_attempt_id'] or item['recall_attempt_id']!=item['recall_attempt_id'].strip()
                    or len(item['recall_attempt_id'])>512 or not found_features_valid(item['features'])
                    or type(item['recorded']) is not dict or set(item['recorded'])!={'positive','negative'}
                    or any(type(v) is not bool for v in item['recorded'].values())):
                raise MemoryRequestRejectedError('found features unavailable')
            seen.add(item['admitted_event_id'])
        if result['next_after'] is not None and (type(result['next_after']) is not str or len(result['next_after'])>512 or result['state']!='partial'):
            raise MemoryRequestRejectedError('found features unavailable')
        return result

    async def _host_helped_selector_page(self, origin, *, after=None, limit=64):
        import re
        if (type(origin) is not _HostObservationProjection or not self._host_helped_ready()
                or type(limit) is not int or not 1<=limit<=64 or (after is not None and (type(after) is not str or len(after)>512))):
            raise MemoryRequestRejectedError('helped selector unavailable')
        witness=self._helped_witness
        from src.openclank.mimo_supervisor import _queued_owned_thread
        await _queued_owned_thread(witness[4].assert_valid)
        if self._helped_witness is not witness or not self._host_helped_ready(): raise MemoryRequestRejectedError('helped selector unavailable')
        authority={key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
        request={'version':1,'owner':origin.owner,'workspace_id':origin.workspace_id,'account_id':origin.source.account_id,
                 'source':asdict(origin.source),'authority':authority,'after':after,'limit':limit}
        result=await self._s07_owner_rpc(origin.owner,origin.workspace_id,{'phase':'helped_selector_page','request':request},asyncio.get_running_loop().time()+2.0)
        if (self._helped_witness is not witness or not self._host_helped_ready() or type(result) is not dict
                or set(result)!={'version','state','items','next_after'} or result['version']!=1
                or result['state'] not in ('ready','partial') or type(result['items']) is not list or len(result['items'])>limit):
            raise MemoryRequestRejectedError('helped selector unavailable')
        total=0; seen=set()
        for item in result['items']:
            if (type(item) is not dict or set(item)!={'event_id','signal','memory','display','recorded'}
                    or type(item['event_id']) is not str or not re.fullmatch(r'cs_s06(?:admitted|context)_[0-9a-f]{64}',item['event_id'])
                    or item['event_id'] in seen or item['signal'] not in ('tool_admitted','context_admitted')
                    or (item['signal']=='tool_admitted') != item['event_id'].startswith('cs_s06admitted_')
                    or type(item['memory']) is not dict or set(item['memory'])!={'version','provider','owner','workspace_id','project_id','block_id','revision','value_sha256'}
                    or type(item['memory']['version']) is not int or item['memory']['version']!=1 or item['memory']['provider']!='fm-v2'
                    or type(item['memory']['owner']) is not str or not item['memory']['owner'] or item['memory']['workspace_id']!=origin.workspace_id
                    or (item['memory']['project_id'] is not None and (type(item['memory']['project_id']) is not str or not item['memory']['project_id']))
                    or type(item['memory']['block_id']) is not str or not item['memory']['block_id'] or len(item['memory']['block_id'])>512
                    or type(item['memory']['revision']) is not int or item['memory']['revision']<1
                    or type(item['memory']['value_sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',item['memory']['value_sha256'])
                    or type(item['display']) is not str or len(item['display'].encode())>4096
                    or hashlib.sha256(item['display'].encode()).hexdigest()!=item['memory']['value_sha256']
                    or type(item['recorded']) is not dict or set(item['recorded'])!={'positive','negative'}
                    or any(type(v) is not bool for v in item['recorded'].values())):
                raise MemoryRequestRejectedError('helped selector unavailable')
            seen.add(item['event_id']);total+=len(item['display'].encode())
        if total>32768 or (result['next_after'] is not None and (type(result['next_after']) is not str or len(result['next_after'])>512 or result['state']!='partial')):
            raise MemoryRequestRejectedError('helped selector unavailable')
        return result

    async def _host_append_helped(self, request, *, source, guard, origin, deadline):
        if not self._host_helped_ready() or guard is None or origin.source != source:
            raise MemoryRequestRejectedError('helped write unavailable')
        witness=self._helped_witness
        from src.openclank.mimo_supervisor import _queued_owned_thread
        await _queued_owned_thread(witness[4].assert_valid)
        if self._helped_witness is not witness or not self._host_helped_ready(): raise MemoryRequestRejectedError('helped write unavailable')
        result=await self._host_candidate_request(request['owner'],request['workspace_id'],{'phase':'helped_observation','request':request},require_live=True,write_deadline=float(deadline))
        if self._helped_witness is not witness or not self._host_helped_ready(): raise MemoryRequestRejectedError('helped outcome unknown')
        return self._validate_helped_receipt(result,request)

    async def _host_helped_status(self, request, *, source, guard, origin, deadline):
        if not self._host_helped_ready() or guard is None or origin.source != source:
            raise MemoryRequestRejectedError('helped status unavailable')
        witness=self._helped_witness
        from src.openclank.mimo_supervisor import _queued_owned_thread
        await _queued_owned_thread(witness[4].assert_valid)
        if self._helped_witness is not witness or not self._host_helped_ready(): raise MemoryRequestRejectedError('helped status unavailable')
        current={key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
        result=await self._s07_owner_rpc(request['owner'],request['workspace_id'],{'phase':'helped_status','request':request,'current_authority':current},deadline)
        if self._helped_witness is not witness or not self._host_helped_ready(): raise MemoryRequestRejectedError('helped status unavailable')
        return self._validate_helped_receipt(result,request)

    @staticmethod
    def _validate_helped_receipt(result,request):
        import re
        expected='cs_s06helped_'+hashlib.sha256(json.dumps(['openclank.s06.helped.id.v1',request['account_id'],request['request_key']],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if (type(result) is not dict or set(result)!={'version','state','event_id','receipt_sha256'} or result['version']!=1 or result['state']!='applied'
                or result['event_id']!=expected
                or type(result['receipt_sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',result['receipt_sha256'])):
            raise MemoryRequestRejectedError('helped receipt unavailable')
        return result

    async def _host_event_export_page(self, origin: _HostObservationProjection, *, kind: str, after: str | None = None, limit: int = 128):
        """Read one source-bound historical page; caller supplies fresh host guard."""
        import re
        from src.openclank.memory_job_admission import _conn
        from src.memery_external_sources import SourceRef, _source_ref_error
        if (type(origin) is not _HostObservationProjection or type(origin.source) is not SourceRef
            or _provider_source_ref_error(self, origin.source) or type(origin.owner) is not str or not origin.owner
            or origin.workspace_id != self._workspace_id or self._scope(origin.owner).owner != origin.owner
            or kind not in ('found', 'admitted', 'helped') or type(limit) is not int or not 1 <= limit <= 128
            or (after is not None and (type(after) is not str or not after or len(after)>512 or after!=after.strip()))):
            raise MemoryRequestRejectedError('observation export unavailable')
        if kind=='helped' and (not callable(getattr(self,'_host_helped_ready',None)) or not self._host_helped_ready()):
            raise MemoryRequestRejectedError('helped export unavailable')
        try:
            with _conn(self._local_db_path()) as db:
                if not self._s06_source_capability(db) or (kind=='admitted' and not self._s06_admitted_capability(db)):
                    raise ValueError('observation export capability unavailable')
        except (ValueError,TypeError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('observation export unavailable') from exc
        authority={key:getattr(origin,key) for key in ('origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash')}
        result=await self._host_candidate_request(origin.owner,origin.workspace_id,
            {'phase':'observation_export_page','source':asdict(origin.source),'authority':authority,
             'kind':kind,'after':after,'limit':limit},require_live=True)
        pattern={'found':r'cs_s06found_[0-9a-f]{64}','admitted':r'cs_s06admitted_[0-9a-f]{64}','helped':r'cs_s06helped_[0-9a-f]{64}'}[kind]
        if (type(result) is not dict or set(result)!={'events','next_after'} or type(result['events']) is not list
            or len(result['events'])>limit):
            raise MemoryRequestRejectedError('observation export response unavailable')
        for event in result['events']:
            if (type(event) is not dict or set(event)!=({'kind','event_id','block_id','revision','receipt_sha256','source_id','origin_id'} | ({'polarity'} if kind=='helped' else set()))
                or (kind=='helped' and event.get('polarity') not in ('positive','negative'))
                or event['kind'] not in ((kind, 'context_admitted') if kind == 'admitted' else (kind,)) or type(event['event_id']) is not str or not re.fullmatch((r'cs_s06context_[0-9a-f]{64}' if event['kind']=='context_admitted' else pattern),event['event_id'])
                or type(event['block_id']) is not str or not event['block_id'] or len(event['block_id'])>512
                or type(event['revision']) is not int or event['revision']<1
                or type(event['receipt_sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',event['receipt_sha256'])
                or event['origin_id'] != origin.origin_id or type(event['source_id']) is not str
                or not re.fullmatch(r'hostpart_[0-9a-f]{64}',event['source_id'])):
                raise MemoryRequestRejectedError('observation export response unavailable')
        cursor=result['next_after']
        if cursor is not None and (type(cursor) is not str or len(cursor)>512 or not cursor.startswith('outbox_cs_s06')
                                   or (after is not None and cursor<=after)):
            raise MemoryRequestRejectedError('observation export cursor unavailable')
        return {'events':tuple(result['events']), 'next_after':cursor}

    async def _s07_owner_rpc(self, owner: str, workspace: str, payload: dict, deadline: float, *, read_ticket=None, _purge_capability=None, _paired_current=None, _s09_digest_lease=None, _s02_current=None):
        """Try one optional RPC without waiting behind existing owner work."""
        purge_current=None;s09_current=None
        if type(payload) is dict and payload.get('phase')=='question_digest':
            request=payload.get('request')
            if (type(_s09_digest_lease) is not _S09DigestLease or type(read_ticket) is not dict
                    or set(payload)!={'phase','request'} or type(request) is not dict
                    or request!={'version':1,'owner':owner,'workspace_id':workspace,'account_id':_s09_digest_lease.account}
                    or type(request.get('version')) is not int or _s09_digest_lease.owner!=owner
                    or _s09_digest_lease.workspace!=workspace or _s09_digest_lease.provider is not self
                    or not _s09_digest_lease.current()):return None
            s09_current=_s09_digest_lease.current
        elif _s09_digest_lease is not None:
            return None
        if type(payload) is dict and payload.get('phase')=='paired_erased_fact':
            request=payload.get('request')
            if (not callable(_paired_current) or type(read_ticket) is not dict or set(payload)!={'phase','request'}
                    or type(request) is not dict or set(request)!={'version','owner','account_id','workspace_id','job_id'}
                    or type(request.get('version')) is not int or request['version']!=1
                    or request.get('owner')!=owner or request.get('workspace_id')!=workspace or _paired_current() is not True):
                return None
        elif _paired_current is not None:
            return None
        s02_phases = ('s02_importance_capability','s02_importance_context','s02_importance_resolve','s02_importance_review_context','s02_importance_target_manifest','s02_importance_original')
        if type(payload) is dict and payload.get('phase') in s02_phases:
            if not callable(_s02_current) or type(read_ticket) is not dict or _s02_current() is not True: return None
        elif _s02_current is not None:
            return None
        if type(payload) is dict and payload.get('phase')=='owner_purge_stats':
            from src.openclank.chat_source_bridge import HostAccountSourceTransition
            request=payload.get('request')
            if (type(_purge_capability) is not HostAccountSourceTransition or type(read_ticket) is not dict
                    or set(payload)!={'phase','request'} or type(request) is not dict
                    or set(request)!={'version','owner','account_id'} or type(request.get('version')) is not int
                    or request['version']!=1 or request.get('owner')!=owner):
                return None
            purge_transport=self._owned_transport;purge_generation=self._owned_generation;purge_task=self._owner_task
            purge_process=getattr(purge_transport,'process',None);purge_preparation=self._guarded_preparation
            if purge_transport is None or purge_process is None or purge_task is None or purge_preparation is None:
                return None
            def purge_current():
                try:
                    if (self._owned_transport is not purge_transport or self._owned_generation is not purge_generation
                            or self._owner_task is not purge_task or purge_task.done() or self._guarded_preparation is not purge_preparation
                            or purge_transport.process is not purge_process or purge_process.returncode is not None
                            or not purge_transport.current(purge_generation)):
                        return False
                    operation=_purge_capability.lifecycle.get_operation(_purge_capability.operation_id)
                    if operation.get('subject_id')!=request.get('account_id'): return False
                    _purge_capability.assert_current(provider=self,action='purge',owner=owner)
                    return True
                except Exception:return False
            if purge_current() is not True:return None
        elif _purge_capability is not None:
            return None
        try: self._assert_reset_handoff_access()
        except MemoryRequestRejectedError: return None
        loop = asyncio.get_running_loop()
        s07_phase = payload.get('phase') if payload.get('phase') in ('s07_recall_start','s07_recall_poll','s07_revalidate_start') else None
        s07_trace = bool(s07_phase and logger.isEnabledFor(logging.DEBUG))
        s07_entered = loop.time() if s07_trace else None
        if (not self._guarded_v17 or self._broker_url or self._local_db_path() is None or workspace != self._workspace_id
                or self._owned_poisoned or self._bounded_write_reserved
                or not self._owner_task or self._owner_task.done() or not self._ready or not self._ready.done()
                or self._ready.cancelled() or self._ready.exception() is not None or self._requests is None
                or self._owner_rpc_busy or self._s07_rpc_reserved or not self._requests.empty() or loop.time() >= deadline):
            if s07_trace: logger.debug('S07_NATIVE phase=%s stage=preflight_refused elapsed_ms=%.3f',s07_phase,(loop.time()-s07_entered)*1000)
            return None
        fut = loop.create_future(); fut._s07_deadline = deadline
        if s07_trace: fut._s07_debug_queued = loop.time()
        if read_ticket is not None:
            from src.openclank.fm_owned_stdio import OwnedStdio
            transport=self._owned_transport;generation=self._owned_generation
            if (type(read_ticket) is not dict or payload.get('phase') not in ('question_digest','owner_purge_stats','paired_erased_fact','code_library_observe','code_manifest_compare','document_segments','document_structure_read','document_vector_intent_status','document_vector_batch_status','document_vector_intent_text','document_vector_selected_start','document_vector_selected_status','document_vector_selected_search','document_vector_search','document_vector_head','document_vector_text','document_remove_original','document_source_lookup','document_vector_snapshot','document_upsert_original','prepared_knowledge_read','mirror_context','paired_review_original','paired_original_page','paired_review_page','paired_reset_fence_read','selective_ingest_partition','explicit_add_status','explicit_add_review_preview','explicit_add_review_status','guarded_full_memory_preview','guarded_selective_ingest_preview','explicit_add_review_original','explicit_add_missing_original_page','explicit_add_missing_terminal_page','import_review_status','import_staged_review_preview','import_staged_review_status','import_staged_review_original','import_missing_original_page','import_missing_terminal_page','explicit_add_capability','pending_pair_original','source_publication_original','paired_publication_original','s02_rename_assessment','s02_importance_capability','s02_importance_context','s02_importance_resolve','s02_importance_review_context','s02_importance_target_manifest','s02_importance_original','s08_policy_capability','skill_draft_capability','connection_predicate_options','connection_review_manifest','s08_policy_review_manifest','s08_policy_review_reject_preview','s08_policy_review_original','s08_review_kind','s08_original_scoped','s05_reserve_original','s05_reserve_readiness','skill_draft_retire_original','skill_draft_prepare_original','skill_draft_reserve_original','skill_draft_original','skill_draft_result_original','s08_model_prepare_original','s08_model_reserve_original','s08_model_original','s08_policy_result_original','native_memory_ops_capability','native_memory_ops_batch_preview','native_memory_ops_batch_status','preextracted_list','preextracted_review_preview','s08_last_retained_run','m3_rank','knowledge_update_batch_preview','knowledge_update_batch_status','journal_delta_status','guarded_batch_forget_preview','guarded_batch_forget_status')
                or type(transport) is not OwnedStdio or not transport.current(generation)):
                return None
            read_ticket.update(transport=transport,generation=generation,issued=False,settled=loop.create_future())
            if s09_current is not None:
                read_ticket.update(strict_frame=True,frame_check=s09_current,deadline=deadline)
            elif purge_current is not None:
                read_ticket.update(strict_frame=True,frame_check=purge_current,deadline=deadline)
            elif _paired_current is not None:
                read_ticket.update(strict_frame=True,frame_check=_paired_current,deadline=deadline)
            elif _s02_current is not None:
                read_ticket.update(strict_frame=True,frame_check=_s02_current,deadline=deadline)
            fut._owned_read_ticket=read_ticket
        self._s07_rpc_reserved = True
        if payload.get('phase') in ('guarded_batch_forget_preview','guarded_batch_forget_status','prepared_knowledge_read','metadata_read','domain_definition_list','helped_selector_page','account_value_successor_preview','account_value_successor_status','analysis_snapshot_status','standing_guidance_preview','standing_guidance_read','standing_guidance_status','standing_guidance_select','standing_guidance_revalidate','explicit_add_status','import_review_status','pending_pair_original','source_publication_original','paired_publication_original','s02_rename_assessment'):
            nested=payload.get('request')
            if (set(payload)!={'phase','request'} or type(nested) is not dict or nested.get('owner')!=owner or nested.get('workspace_id')!=workspace):
                self._s07_rpc_reserved=False
                return None
            envelope=payload
        elif payload.get('phase') in ('explicit_add_review_status','import_staged_review_status'):
            nested=payload.get('request');selector=nested.get('selector') if type(nested) is dict else None
            if (set(payload)!={'phase','request'} or type(selector) is not dict or selector.get('owner')!=owner or selector.get('workspace_id')!=workspace):
                self._s07_rpc_reserved=False
                return None
            envelope=payload
        elif payload.get('phase') in ('native_memory_ops_batch_preview','native_memory_ops_batch_status','preextracted_list','preextracted_review_preview','s08_last_retained_run','m3_rank','knowledge_update_batch_preview','knowledge_update_batch_status','journal_delta_status','explicit_add_review_preview','explicit_add_review_original','explicit_add_missing_original_page','explicit_add_missing_terminal_page','import_staged_review_preview','import_staged_review_original','import_missing_original_page','import_missing_terminal_page'):
            nested=payload.get('request')
            if (set(payload)!={'phase','request'} or type(nested) is not dict or nested.get('owner')!=owner or nested.get('workspace_id')!=workspace):
                self._s07_rpc_reserved=False
                return None
            envelope=payload
        elif payload.get('phase') == 'paired_reset_fence_read':
            nested=payload.get('request')
            if (set(payload)!={'phase','request','reset'} or type(nested) is not dict or nested.get('owner')!=owner or nested.get('workspace_id')!=workspace or type(payload.get('reset')) is not dict):
                self._s07_rpc_reserved=False
                return None
            envelope=payload
        elif payload.get('phase') == 'helped_status':
            nested=payload.get('request')
            if (set(payload)!={'phase','request','current_authority'} or type(nested) is not dict or nested.get('owner')!=owner or nested.get('workspace_id')!=workspace):
                self._s07_rpc_reserved=False
                return None
            envelope=payload
        elif payload.get('phase')=='owner_purge_stats':
            envelope={'owner':owner,'workspace_id':workspace,**payload}
        elif payload.get('phase') in _S05_RESERVE_READS:
            try: envelope=_s05_reserve_envelope(payload,owner,workspace)
            except (TypeError,ValueError,UnicodeError,OverflowError):
                self._s07_rpc_reserved=False
                return None
        elif payload.get('phase') in _SKILL_DRAFT_READS:
            try: envelope=_skill_draft_envelope(payload,owner,workspace)
            except (TypeError,ValueError,UnicodeError,OverflowError):
                self._s07_rpc_reserved=False
                return None
        else:
            envelope={'owner':owner,'workspace_id':workspace,**payload}
        try: self._assert_reset_handoff_access()
        except MemoryRequestRejectedError:
            self._s07_rpc_reserved=False
            return None
        self._requests.put_nowait((_HOST_CANDIDATE_REVIEW, envelope, fut))
        try:
            done, _ = await asyncio.wait((fut,self._owner_task), timeout=max(0,deadline-loop.time()), return_when=asyncio.FIRST_COMPLETED)
            if s07_trace: logger.debug('S07_NATIVE phase=%s stage=owner_wait elapsed_ms=%.3f done=%s',s07_phase,(loop.time()-s07_entered)*1000,fut in done)
            if fut not in done or fut.cancelled(): return None
            decoded, wire_size = fut.result()
            if loop.time() >= deadline: return None
            limit = 8192 if payload.get('phase') in ('document_remove_original','document_source_lookup','document_upsert_original') else 1048576
            if wire_size > limit: return None
            if s07_trace: logger.debug('S07_NATIVE phase=%s stage=decode_complete elapsed_ms=%.3f',s07_phase,(loop.time()-s07_entered)*1000)
            return decoded if type(decoded) is dict and loop.time() < deadline else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None
        finally:
            if not fut.done(): fut.cancel()
            if read_ticket is not None and read_ticket.get('issued'):
                settled=read_ticket['settled']
                while not settled.done():
                    try:await asyncio.shield(settled)
                    except asyncio.CancelledError:continue
                settled.result()

    async def _host_code_library_observe(self, *, owner, binding, package, deadline, current):
        """Private bounded on-disk Code observation; Human Files remains authority."""
        if (not callable(current) or current() is not True or type(binding) is not dict
                or binding.get('owner')!=owner or binding.get('origin') not in ('app','agent')
                or type(package) is not str or len(package)>214
                or re.fullmatch(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*',package) is None
                or '..' in package): return None
        prepared=self._guarded_preparation; transport=self._owned_transport
        generation=self._owned_generation; task=self._owner_task; ticket={}
        result=await self._s07_owner_rpc(owner,self._workspace_id,
            {'phase':'code_library_observe','request':{'binding':copy.deepcopy(binding),'package':package}},deadline,read_ticket=ticket)
        if (current() is not True or prepared is None or self._guarded_preparation is not prepared
                or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or ticket.get('frame_seen') is not True
                or ticket.get('frame_invalid') is not False or ticket.get('terminal_error') is True
                or type(result) is not dict): return None
        return result

    async def _host_code_manifest_compare(self, *, owner, binding, receipt, source, expected_spec_sha256, deadline, current):
        """Private owned-read phase; caller retains the Human Files authority."""
        if (not callable(current) or current() is not True or type(binding) is not dict
                or binding.get('owner') != owner or binding.get('origin') != 'app'
                or type(receipt) is not dict or type(source) is not dict
                or type(expected_spec_sha256) is not str
                or re.fullmatch(r'[0-9a-f]{64}',expected_spec_sha256) is None):
            return None
        prepared=self._guarded_preparation; transport=self._owned_transport
        generation=self._owned_generation; task=self._owner_task; ticket={}
        request={'binding':copy.deepcopy(binding),'receipt':copy.deepcopy(receipt),
                 'source':copy.deepcopy(source),'expected_spec_sha256':expected_spec_sha256}
        result=await self._s07_owner_rpc(owner,self._workspace_id,
            {'phase':'code_manifest_compare','request':request},deadline,read_ticket=ticket)
        if (current() is not True or prepared is None or self._guarded_preparation is not prepared
                or self._owned_transport is not transport or self._owned_generation is not generation
                or self._owner_task is not task or ticket.get('frame_seen') is not True
                or ticket.get('frame_invalid') is not False or ticket.get('terminal_error') is True
                or type(result) is not dict or set(result)!={'state','reason','manifest_comparison'}
                or result.get('state')!='current' or type(result.get('manifest_comparison')) is not dict):
            return None
        compared=result['manifest_comparison']; state=compared.get('state')
        if state in ('no_match','unsupported') and set(compared)=={'state'}: return compared
        fields={'state','key_start','key_end','value_start','value_end'}
        if (state=='match' and set(compared)==fields and all(type(compared[k]) is int and 0<=compared[k]<=524288 for k in fields-{'state'})
                and compared['key_start']<compared['key_end']<=compared['value_start']<compared['value_end']):
            return compared
        return None

    async def _host_prepared_knowledge_read(self, *, owner, workspace_id, account_id, policy_projection, limit, cursor, expected, deadline, read_ticket=None, curated_id=None):
        """Optional bounded private read; callers retain current host authority."""
        if (any(type(v) is not str or not v or len(v)>512 for v in (owner,workspace_id,account_id)) or
            type(policy_projection) is not dict or type(limit) is not int or not 1<=limit<=32 or
            (cursor is not None and expected is not None) or
            (curated_id is not None and (cursor is not None or expected is not None or type(curated_id) is not str or not curated_id or curated_id != curated_id.strip() or len(curated_id.encode('utf-8'))>512 or any(ord(c)<32 or ord(c)==127 for c in curated_id)))):
            return None
        request={'version':1,'owner':owner,'workspace_id':workspace_id,'account_id':account_id,
                 'policy_projection':policy_projection,'limit':limit,'cursor':cursor,'expected':expected}
        if curated_id is not None: request['curated_id']=curated_id
        value=await self._s07_owner_rpc(owner,workspace_id,{'phase':'prepared_knowledge_read','request':request},deadline,read_ticket=read_ticket)
        if type(value) is not dict or type(value.get('version')) is not int or value.get('version')!=1:
            return None
        if value.get('state')=='stale':
            return value if set(value)=={'version','state','reason'} and value['reason']=='restart' else None
        def item_valid(item):
            if (type(item) is not dict or set(item)!={'selected','trust','record_type'} or type(item['trust']) not in (int,float)
                or not math.isfinite(item['trust']) or type(item['record_type']) is not str or not item['record_type']
                or len(item['record_type'].encode('utf-8'))>128): return False
            selected=item['selected']
            if type(selected) is not dict or set(selected)!={'curated_id','reference'} or type(selected['curated_id']) is not str or not selected['curated_id'] or len(selected['curated_id'])>512: return False
            ref=selected['reference']
            return (type(ref) is dict and set(ref)=={'version','provider','owner','workspace_id','project_id','block_id','revision','value_sha256'}
                and type(ref['version']) is int and ref['version']==1 and ref['provider']=='fm-v2' and ref['owner']==owner
                and ref['workspace_id']==workspace_id and ref['project_id'] is None and type(ref['block_id']) is str and bool(ref['block_id'])
                and len(ref['block_id'])<=512 and type(ref['revision']) is int and ref['revision']>0
                and type(ref['value_sha256']) is str and re.fullmatch(r'[0-9a-f]{64}',ref['value_sha256']) is not None)
        if curated_id is not None:
            if (set(value)!={'version','state','item','body'} or value['state']!='ready' or not item_valid(value['item'])
                or value['item']['selected']['curated_id']!=curated_id or type(value['body']) is not str or len(value['body'].encode('utf-8'))>16384): return None
        elif expected is not None:
            if (set(value)!={'version','state','item','body'} or value['state']!='ready' or not item_valid(value['item']) or value['item']!=expected
                or type(value['body']) is not str or len(value['body'].encode('utf-8'))>16384): return None
        else:
            if (set(value)!={'version','state','items','generation','next','complete'} or value['state'] not in ('ready','partial')
                or type(value['items']) is not list or len(value['items'])>limit or not all(item_valid(v) for v in value['items'])
                or type(value['generation']) is not str or re.fullmatch(r'[0-9a-f]{64}',value['generation']) is None
                or type(value['complete']) is not bool or (value['state']=='partial' and value['complete'])): return None
            keys=[item['selected']['curated_id'] for item in value['items']]
            if keys!=sorted(set(keys)) or (value['state']=='ready' and value['next'] is None and not value['complete']): return None
            next_cursor=value['next']
            if next_cursor is not None and (type(next_cursor) is not dict or set(next_cursor)!={'generation','last_key'}
                or next_cursor['generation']!=value['generation'] or not value['items'] or next_cursor['last_key']!=value['items'][-1]['selected']['curated_id'] or value['complete']): return None
        return value

    async def _host_value_successor_context(self, *, owner, workspace_id, account_id, curated_id):
        result=await self._host_candidate_request(owner,workspace_id,{'phase':'mirror_context','curated_id':curated_id,'account_id':account_id})
        if type(result) is not dict or result.get('mode')!='guarded' or result.get('reviewer_account_id')!=account_id or result.get('snapshot_copy') is not None:
            return None
        binding=result.get('binding')
        if (type(binding) is not dict or binding.get('owner')!=owner or binding.get('workspace')!=workspace_id
            or binding.get('project')!='' or binding.get('curated_id')!=curated_id or type(binding.get('revision')) is not int
            or result.get('expected_revision')!=binding['revision'] or any(type(result.get(k)) is not str or not result[k] for k in ('candidate_id','origin_request_key','prior_receipt_id'))):
            return None
        return {key:value for key,value in result.items() if key!='reviewer_account_id'}

    async def _host_analysis_snapshot_capability(self, *, owner, workspace_id, deadline, request_version=1):
        if type(request_version) is not int or request_version not in (1,2):
            return False
        payload={'phase':'analysis_snapshot_capability'}
        if request_version==2: payload['request_version']=2
        result=await self._s07_owner_rpc(owner,workspace_id,payload,deadline)
        return result=={'version':request_version,'state':'ready','phase':'analysis_snapshot_publish'}

    async def _host_analysis_snapshot_publish(self, *, owner, workspace_id, request, deadline, write_status=None):
        if type(request) is not dict or request.get('owner')!=owner or request.get('workspace_id')!=workspace_id or not self._host_bounded_write_available():
            raise MemoryRequestRejectedError('snapshot unavailable')
        from src.openclank.analysis_snapshot_contract import canonical_json
        result=await self._host_candidate_request(owner,workspace_id,{'phase':'analysis_snapshot_publish','request':request},require_live=True,write_deadline=deadline,write_status=write_status)
        checked=self._validate_analysis_snapshot_receipt(result,{**request,'request_sha256':hashlib.sha256(canonical_json(request)).hexdigest()})
        if checked['manifest']!=request.get('manifest') or checked['capture_origin']!=request.get('capture_origin'):
            raise MemoryRequestRejectedError('snapshot receipt unavailable')
        return checked

    async def _host_analysis_snapshot_status(self, *, owner, workspace_id, request, deadline):
        if type(request) is not dict or request.get('owner')!=owner or request.get('workspace_id')!=workspace_id:
            raise MemoryRequestRejectedError('snapshot unavailable')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'analysis_snapshot_status','request':request},deadline)
        if result=={'state':'unknown'}:
            return result
        return self._validate_analysis_snapshot_receipt(result,request)

    @staticmethod
    def _validate_analysis_snapshot_receipt(result, request):
        if (type(result) is not dict or set(result)!={'state','request_key','request_sha256','manifest','capture_origin','receipt_id','curated_id','revision'}
            or result.get('state')!='applied' or result.get('request_key')!=request.get('request_key')
            or (request.get('request_sha256') is not None and result.get('request_sha256')!=request['request_sha256'])
            or type(result.get('request_sha256')) is not str or type(result.get('manifest')) is not dict
            or result.get('capture_origin') not in ('verified_capture','imported_unverified')
            or type(result.get('receipt_id')) is not str or type(result.get('curated_id')) is not str or not result['curated_id'] or result.get('revision')!=1):
            raise MemoryRequestRejectedError('snapshot receipt unavailable')
        return result

    @staticmethod
    def _standing_guidance_requirement(value, message='guidance unavailable'):
        # Closed shape, mirrored from Native (C3-P1): one effect, one denied audience.
        if (type(value) is not dict or set(value)!={'version','effect','deny'} or type(value['version']) is not int or value['version']!=1
            or value['effect']!='publish_file' or type(value['deny']) is not dict or set(value['deny'])!={'audience'} or value['deny']['audience']!='public'):
            raise MemoryRequestRejectedError(message)
        return value

    @staticmethod
    def _validate_standing_guidance_items(items):
        if type(items) is not list or len(items)>4: raise MemoryRequestRejectedError('guidance unavailable')
        seen_curated=set();seen_blocks=set();total=0
        for item in items:
            if type(item) is dict and 'requirement' in item: MemeryProvider._standing_guidance_requirement(item['requirement'])
            if (type(item) is not dict or set(item)-{'requirement'}!={'curated_id','block_id','revision','receipt_id','receipt_sha256','value_sha256','text'}
                or any(type(item[k]) is not str or not item[k] or len(item[k])>512 for k in ('curated_id','block_id','receipt_id'))
                or type(item['revision']) is not int or item['revision']<1
                or re.fullmatch(r'cs_s10_purpose_[0-9a-f]{64}',item['receipt_id']) is None
                or any(type(item[k]) is not str or re.fullmatch(r'[0-9a-f]{64}',item[k]) is None for k in ('receipt_sha256','value_sha256'))
                or type(item['text']) is not str or not item['text'].strip()):raise MemoryRequestRejectedError('guidance unavailable')
            try: raw=item['text'].encode('utf-8','strict')
            except UnicodeError as exc:raise MemoryRequestRejectedError('guidance unavailable') from exc
            if (len(raw)>2048 or hashlib.sha256(raw).hexdigest()!=item['value_sha256'] or item['curated_id'] in seen_curated or item['block_id'] in seen_blocks):raise MemoryRequestRejectedError('guidance unavailable')
            seen_curated.add(item['curated_id']);seen_blocks.add(item['block_id']);total+=len(raw)
        if total>8192:raise MemoryRequestRejectedError('guidance unavailable')

    async def _host_standing_guidance_select(self, *, owner, workspace_id, account_id, policy_projection, deadline):
        try: frozen_policy=json.loads(json.dumps(policy_projection,ensure_ascii=False,allow_nan=False))
        except (TypeError,ValueError,UnicodeError) as exc:raise MemoryRequestRejectedError('guidance unavailable') from exc
        request={'version':1,'owner':owner,'workspace_id':workspace_id,'account_id':account_id,'policy_projection':frozen_policy}
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'standing_guidance_select','request':request},deadline)
        if type(result) is not dict or set(result)!={'version','state','coverage','items'} or result.get('version')!=1 or result.get('state')!='ready' or result.get('coverage') not in ('complete','partial'):
            raise MemoryRequestRejectedError('guidance unavailable')
        self._validate_standing_guidance_items(result['items']);return result

    async def _host_standing_guidance_revalidate(self, *, owner, workspace_id, account_id, policy_projection, items, deadline):
        self._validate_standing_guidance_items(items)
        try: frozen_policy=json.loads(json.dumps(policy_projection,ensure_ascii=False,allow_nan=False))
        except (TypeError,ValueError,UnicodeError) as exc:raise MemoryRequestRejectedError('guidance unavailable') from exc
        request={'version':1,'owner':owner,'workspace_id':workspace_id,'account_id':account_id,'policy_projection':frozen_policy,'items':[dict(item) for item in items]}
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'standing_guidance_revalidate','request':request},deadline)
        if result!={'version':1,'state':'current'}:raise MemoryRequestRejectedError('guidance unavailable')
        return result

    async def _host_standing_guidance(self, *, owner, workspace_id, phase, request=None, deadline):
        if phase not in ('capability','preview','read','apply','status'):
            raise MemoryRequestRejectedError('purpose unavailable')
        if phase=='capability':
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'standing_guidance_capability'},deadline)
            return result=={'version':1,'state':'ready','phase':'standing_guidance_apply'}
        if type(request) is not dict or request.get('owner')!=owner or request.get('workspace_id')!=workspace_id:
            raise MemoryRequestRejectedError('purpose unavailable')
        if 'requirement' in request:  # optional; only an approve decision may carry it, never invented here
            self._standing_guidance_requirement(request['requirement'],'purpose unavailable')
            if request.get('decision','approve')!='approve': raise MemoryRequestRejectedError('purpose unavailable')
        payload={'phase':'standing_guidance_'+phase,'request':request}
        if phase in ('apply','override_apply'):
            if not self._host_bounded_write_available(): raise MemoryRequestRejectedError('purpose unavailable')
            result=await self._host_candidate_request(owner,workspace_id,payload,require_live=True,write_deadline=float(deadline))
            if (type(result) is not dict or set(result)!={'state','receipt_id','revision','decision'} or result.get('state')!='applied'
                or type(result.get('receipt_id')) is not str or result.get('revision')!=request.get('expected_revision',-2)+1 or result.get('decision')!=request.get('decision')):
                raise MemoryRequestRejectedError('purpose receipt unavailable')
        else:
            result=await self._s07_owner_rpc(owner,workspace_id,payload,deadline)
            if type(result) is not dict: raise MemoryRequestRejectedError('purpose unavailable')
            if phase=='preview' and (set(result)!={'version','state','request','selector','value'} or result.get('version')!=1 or result.get('state')!='preview'
                or type(result.get('request')) is not dict or type(result.get('selector')) is not dict or type(result.get('value')) is not str
                or result['request'].get('requirement')!=request.get('requirement') or ('requirement' in result['request'])!=('requirement' in request)):
                raise MemoryRequestRejectedError('purpose preview unavailable')
            if phase=='read' and (set(result)!={'version','state','decision','revision'} or result.get('version')!=1 or result.get('state')!='ready'
                or result.get('decision') not in ('approve','withdraw','inactive') or type(result.get('revision')) is not int):
                raise MemoryRequestRejectedError('purpose read unavailable')
            if phase=='status' and not (result=={'state':'unknown'} or (set(result)=={'state','receipt_id','revision','decision','current'}
                and result.get('state')=='applied' and type(result.get('receipt_id')) is str and type(result.get('revision')) is int
                and result.get('decision') in ('approve','withdraw') and type(result.get('current')) is bool)):
                raise MemoryRequestRejectedError('purpose status unavailable')
        return result

    async def _host_value_successor_capability(self, *, owner, workspace_id, deadline):
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'account_value_successor_capability'},deadline)
        return result=={'version':1,'state':'ready','phase':'account_value_successor_apply'}

    async def _host_value_successor_preview(self, *, owner, workspace_id, request, deadline):
        if type(request) is not dict or request.get('owner')!=owner or request.get('workspace_id')!=workspace_id:
            raise MemoryRequestRejectedError('successor unavailable')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'account_value_successor_preview','request':request},deadline)
        if type(result) is not dict or set(result)!={'version','state','request','selector','old_value','old_value_sha256'} or result.get('version')!=1 or result.get('state')!='preview' or type(result.get('request')) is not dict or type(result.get('selector')) is not dict or type(result.get('old_value')) is not str or type(result.get('old_value_sha256')) is not str:
            raise MemoryRequestRejectedError('successor preview unavailable')
        return result

    async def _host_value_successor_apply(self, *, owner, workspace_id, request, deadline):
        if type(request) is not dict or request.get('owner')!=owner or request.get('workspace_id')!=workspace_id or not self._host_bounded_write_available():
            raise MemoryRequestRejectedError('successor unavailable')
        result=await self._host_candidate_request(owner,workspace_id,{'phase':'account_value_successor_apply','request':request},require_live=True,write_deadline=deadline)
        if type(result) is not dict or set(result)!={'state','receipt_id','revision'} or result.get('state')!='applied' or type(result.get('receipt_id')) is not str or type(result.get('revision')) is not int or result['revision']!=request.get('expected_revision',-2)+1:
            raise MemoryRequestRejectedError('successor receipt unavailable')
        return result

    async def _host_value_successor_status(self, *, owner, workspace_id, request, current_policy, deadline):
        if type(request) is not dict or request.get('owner')!=owner or request.get('workspace_id')!=workspace_id or type(current_policy) is not dict:
            raise MemoryRequestRejectedError('successor unavailable')
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'account_value_successor_status','request':{**request,'current_policy':current_policy}},deadline)
        if type(result) is not dict or result.get('state') not in ('unknown','applied') or (result['state']=='unknown' and set(result)!={'state'}) or (result['state']=='applied' and (set(result)!={'state','receipt_id','revision'} or type(result.get('receipt_id')) is not str or type(result.get('revision')) is not int)):
            raise MemoryRequestRejectedError('successor status unavailable')
        return result

    async def _host_account_candidate_capability(self, *, owner, workspace_id, deadline):
        status=await self._s07_owner_rpc(owner,workspace_id,{'phase':'account_candidate_status'},deadline)
        return type(status) is dict and type(status.get('version')) is int and status == {'version':1,'state':'ready','phase':'account_candidate_apply'}

    async def _host_domain_definition_capability(self, *, owner, workspace_id, deadline):
        status=await self._s07_owner_rpc(owner,workspace_id,{'phase':'domain_definition_status'},deadline)
        return type(status) is dict and type(status.get('version')) is int and status=={'version':1,'state':'ready','phase':'domain_definition_review'}

    async def _host_s02_rename_assessment(self,request,*,current_guard):
        if type(request) is not dict or not callable(current_guard) or current_guard() is not True:
            raise MemoryRequestRejectedError('S02 assessment unavailable')
        try:
            raw=json.dumps(request,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            if len(raw.encode('utf-8'))>65536:raise ValueError('request too large')
            request=json.loads(raw)
        except (TypeError,ValueError,OverflowError):raise MemoryRequestRejectedError('S02 assessment unavailable') from None
        owner=request.get('owner');workspace=request.get('workspace_id');account=request.get('account_id');fingerprint=request.get('expected_source_fingerprint')
        if any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (owner,workspace,account)) or type(fingerprint) is not str or re.fullmatch(r'[0-9a-f]{64}',fingerprint) is None:
            raise MemoryRequestRejectedError('S02 assessment unavailable')
        result=await self._s07_owner_rpc(owner,workspace,{'phase':'s02_rename_assessment','request':request},asyncio.get_running_loop().time()+1.5,read_ticket={})
        if result!={'version':1,'state':'assessed','owner':owner,'account_id':account,'source_fingerprint':fingerprint} or type(result.get('version')) is not int or current_guard() is not True:
            raise MemoryRequestRejectedError('S02 assessment unavailable')
        return result

    async def _host_s02_importance_read(self, *, owner, workspace_id, account_id, phase, current, deadline, candidate_id=None, curated_id=None, block_id=None, expected_revision=None, policy=None, original=None):
        """Private exact-owner reads; caller retains the Human/account authority."""
        phases = {'context', 'review_context', 'target_manifest', 'original', 'resolve', 'resolve_target'}
        if (phase not in phases or not callable(current) or type(deadline) is not float or not math.isfinite(deadline)
            or any(type(v) is not str or not v or v != v.strip() or len(v.encode('utf-8')) > 512 for v in (owner, workspace_id, account_id))):
            raise MemoryRequestRejectedError('S02 importance unavailable')
        if current() is not True:
            raise MemoryRequestRejectedError('S02 importance unavailable')
        if phase == 'original':
            original_keys = {'version','owner','account_id','workspace_id','candidate_id','block_id','curated_id','expected_revision','expected_metadata_sha256','predecessor_receipt_id','predecessor_sha256','request_key','reason','importance','policy_projection'}
            if (type(original) is not dict or set(original) != original_keys or type(original.get('version')) is not int or original['version'] != 1
                or type(original.get('owner')) is not str or not original['owner'] or len(original['owner'].encode('utf-8')) > 512
                or original.get('workspace_id') != workspace_id or original.get('account_id') != account_id or type(policy) is not dict
                or type(original.get('expected_revision')) is not int or original['expected_revision'] < 1
                or any(type(original.get(k)) is not str or not original[k] or len(original[k].encode('utf-8')) > 512 for k in ('candidate_id','block_id','curated_id','predecessor_receipt_id','request_key','reason'))
                or any(type(original.get(k)) is not str or re.fullmatch(r'[0-9a-f]{64}',original[k]) is None for k in ('expected_metadata_sha256','predecessor_sha256'))
                or type(original.get('importance')) is not dict or type(original.get('policy_projection')) is not dict):
                raise MemoryRequestRejectedError('S02 importance unavailable')
            payload = {'phase':'s02_importance_original','policy':policy,'request':original}
        elif phase == 'resolve_target':
            if (type(block_id) is not str or not block_id or block_id != block_id.strip() or len(block_id.encode('utf-8')) > 512
                or type(expected_revision) is not int or expected_revision < 1 or type(policy) is not dict):
                raise MemoryRequestRejectedError('S02 importance unavailable')
            payload = {'phase':'s02_importance_resolve','account_id':account_id,'block_id':block_id,'expected_revision':expected_revision,'policy':policy}
        else:
            selector = curated_id if phase == 'resolve' else candidate_id
            if (type(selector) is not str or not selector or selector != selector.strip()
                or len(selector.encode('utf-8')) > 512 or type(policy) is not dict):
                raise MemoryRequestRejectedError('S02 importance unavailable')
            payload = {'phase':'s02_importance_'+phase,'account_id':account_id,('curated_id' if phase == 'resolve' else 'candidate_id'):selector,'policy':policy}
        slot=self._s08_policy_review_ticket if phase=='original' else None
        ticket=slot.get('prepare_ticket') if type(slot) is dict and 's02_importance_preview' in slot else slot
        ticket_phase=None
        snapshot=None
        if ticket is not None:
            owned=ticket.get('owned_original_payload') if type(ticket) is dict else None
            ticket_phase=owned.get('phase') if type(owned) is dict else None
            if ticket_phase not in ('s02_importance_prepare','s02_importance_apply'): return {'version':1,'state':'unknown'}
            try: snapshot=await self._owned_original_checked_snapshot(ticket,ticket_phase,original)
            except (MemoryRequestRejectedError,ValueError): return {'version':1,'state':'unknown'}
        read_ticket={}
        result = await self._s07_owner_rpc(owner, workspace_id, payload, deadline, read_ticket=read_ticket, _s02_current=current)
        if type(result) is not dict or type(result.get('version')) is not int or result.get('version') != 1 or current() is not True:
            raise MemoryRequestRejectedError('S02 importance unavailable')
        keys = {
            'context': {'version','state','target','score_context','typed_value','value_json','snapshot'},
            'resolve_target': {'version','state','target','score_context','typed_value','value_json','snapshot'},
            'review_context': {'version','state','candidate_id','block_id','curated_id','revision','metadata_sha256','predecessor_receipt_id','predecessor_sha256','importance'},
            'resolve': {'version','state','candidate_id','block_id','curated_id','revision','metadata_sha256','predecessor_receipt_id','predecessor_sha256','importance'},
            'target_manifest': {'version','state','proposal_candidate_id','proposal_sha256','root_candidate_id','target','score_context','importance'},
        }
        if phase == 'original':
            if result == {'version':1,'state':'unknown'}: return result
            if result not in ({'version':1,'state':'terminal_erased'}, {'version':1,'state':'account_purged'}) and (set(result) != {'version','state','revision'} or result.get('state') != 'recorded'
                or type(result.get('revision')) is not int or result['revision'] != original['expected_revision']+1):
                raise MemoryRequestRejectedError('S02 importance unavailable')
            if ticket is not None:
                await self._owned_original_checked_current(ticket,ticket_phase,original,snapshot)
                if (current() is not True or self._s08_policy_review_ticket is not slot
                    or read_ticket.get('transport') is not snapshot[1] or read_ticket.get('generation') is not snapshot[2]):
                    raise MemoryRequestRejectedError('S02 importance custody changed')
                self._s08_policy_review_ticket=None
        elif set(result) != keys[phase] or result.get('state') != 'ready':
            raise MemoryRequestRejectedError('S02 importance unavailable')
        elif phase in ('review_context','resolve') and ((phase == 'review_context' and result.get('candidate_id') != candidate_id)
            or (phase == 'resolve' and result.get('curated_id') != curated_id)
            or type(result.get('candidate_id')) is not str or not result['candidate_id']
            or type(result.get('revision')) is not int or result['revision'] < 1
            or any(type(result.get(k)) is not str or not result[k] or len(result[k].encode('utf-8')) > 512 for k in ('block_id','curated_id','predecessor_receipt_id'))
            or any(type(result.get(k)) is not str or re.fullmatch(r'[0-9a-f]{64}',result[k]) is None for k in ('metadata_sha256','predecessor_sha256'))
            or type(result.get('importance')) is not dict):
            raise MemoryRequestRejectedError('S02 importance unavailable')
        elif phase == 'target_manifest' and (result.get('proposal_candidate_id') != candidate_id
            or type(result.get('root_candidate_id')) is not str or not result['root_candidate_id']
            or type(result.get('proposal_sha256')) is not str or re.fullmatch(r'[0-9a-f]{64}',result['proposal_sha256']) is None
            or any(type(result.get(k)) is not dict for k in ('target','score_context','importance'))):
            raise MemoryRequestRejectedError('S02 importance unavailable')
        elif phase in ('context','resolve_target') and (type(result.get('target')) is not dict or type(result.get('score_context')) is not dict
            or type(result.get('snapshot')) is not dict or type(result.get('value_json')) is not str):
            raise MemoryRequestRejectedError('S02 importance unavailable')
        if phase == 'resolve_target' and (result['target'].get('block_id'),result['target'].get('expected_revision')) != (block_id,expected_revision):
            raise MemoryRequestRejectedError('S02 importance unavailable')
        return result

    async def _host_s02_importance_capability(self, *, owner, workspace_id, current, deadline):
        """Read exact Once readiness on the existing live Native lifetime."""
        if not callable(current) or current() is not True or not self._host_s08_policy_ready('importance_target'):
            return False
        transport=self._owned_transport;generation=self._owned_generation
        try:
            result=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s02_importance_capability'},deadline,read_ticket={},_s02_current=current)
            return (result=={'version':1,'state':'ready','profile':'s02.importance-once.v1'}
                and type(result.get('version')) is int and current() is True and self._owned_transport is transport
                and self._owned_generation is generation and self._host_s08_policy_ready('importance_target'))
        except Exception:return False

    async def _host_s02_importance_reserve_preview(self, original, *, owner, workspace_id, current, deadline):
        """Persist one exact original; only a positive Created reply grants this live preview."""
        if (type(original) is not dict or not callable(current) or current() is not True or self._s08_policy_review_ticket is not None
            or not await self._host_s02_importance_capability(owner=owner,workspace_id=workspace_id,current=current,deadline=deadline)):
            raise MemoryRequestRejectedError('S02 durable preview unavailable')
        try:
            raw=json.dumps(original,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(raw)>65536: raise ValueError('original too large')
            frozen=json.loads(raw)
        except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
            raise MemoryRequestRejectedError('S02 durable preview unavailable') from exc
        status={}
        result=await self._host_candidate_request(owner,workspace_id,{'phase':'s02_importance_prepare','request':frozen},require_live=True,write_deadline=deadline,before_issue=current,write_status=status)
        keys={'version','state','prepare_id','prepare_sha256','selector_sha256','request_sha256','root_receipt_id','root_sha256'}
        ticket=status.get('ticket')
        if (type(result) is not dict or set(result)!=keys or type(result.get('version')) is not int or result['version']!=1
            or result.get('state') not in ('created','existing') or type(result.get('prepare_id')) is not str
            or re.fullmatch(r'cs_s02importanceprepare_[0-9a-f]{64}',result['prepare_id']) is None
            or any(type(result.get(k)) is not str or re.fullmatch(r'[0-9a-f]{64}',result[k]) is None for k in ('prepare_sha256','selector_sha256','request_sha256','root_sha256'))
            or type(result.get('root_receipt_id')) is not str or not result['root_receipt_id'] or len(result['root_receipt_id'].encode('utf-8'))>512
            or self._s08_policy_review_ticket is not ticket or current() is not True):
            raise MemoryRequestRejectedError('S02 original outcome unavailable')
        if result['state']=='existing': return {'version':1,'state':'unknown'}
        snapshot=await self._owned_original_checked_snapshot(ticket,'s02_importance_prepare',frozen)
        if current() is not True or self._s08_policy_review_ticket is not ticket or not self._host_s08_policy_ready('importance_target'):
            raise MemoryRequestRejectedError('S02 original authority changed')
        self._s08_policy_review_ticket={'s02_importance_preview':frozen,'preview_transport':snapshot[1],
            'preview_generation':snapshot[2],'prepare_ticket':ticket,'prepare_id':result['prepare_id'],'request_sha256':result['request_sha256']}
        return {'version':1,'state':'created'}

    async def _host_s02_importance_apply(self, *, original, owner, workspace_id, account_id, current, deadline):
        """Issue once under the existing Human review ticket and owned frame."""
        fields={'version','owner','account_id','workspace_id','candidate_id','block_id','curated_id','expected_revision','expected_metadata_sha256','predecessor_receipt_id','predecessor_sha256','request_key','reason','importance','policy_projection'}
        if (not self._host_s08_policy_ready('importance_target')
            or not callable(current) or current() is not True or type(deadline) is not float or not math.isfinite(deadline)
            or type(original) is not dict or set(original)!=fields or type(original.get('version')) is not int or original['version']!=1
            or (original.get('owner'),original.get('account_id'),original.get('workspace_id'))!=(owner,account_id,workspace_id)
            or type(original.get('expected_revision')) is not int or original['expected_revision']<1
            or any(type(original.get(k)) is not str or not original[k] or len(original[k].encode('utf-8'))>512 for k in ('candidate_id','block_id','curated_id','predecessor_receipt_id','request_key','reason'))
            or any(type(original.get(k)) is not str or re.fullmatch(r'[0-9a-f]{64}',original[k]) is None for k in ('expected_metadata_sha256','predecessor_sha256'))
            or type(original.get('policy_projection')) is not dict or type(original.get('importance')) is not dict
            or original['importance'].get('approved_by')!=owner or original['importance'].get('origin') not in ('manual','inferred')):
            raise MemoryRequestRejectedError('S02 importance write unavailable')
        try:
            raw=json.dumps(original,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            if len(raw)>65536: raise ValueError('original too large')
            frozen=json.loads(raw)
        except (TypeError,ValueError,UnicodeError,OverflowError) as exc:
            raise MemoryRequestRejectedError('S02 importance write unavailable') from exc
        preview=self._s08_policy_review_ticket
        if (type(preview) is not dict or preview.get('s02_importance_preview')!=frozen
            or preview.get('preview_transport') is not self._owned_transport or preview.get('preview_generation') is not self._owned_generation
            or current() is not True):
            raise MemoryRequestRejectedError('S02 original status required')
        # No await between consuming the reservation and the existing owned issue;
        # a pre-issue failure still consumes it and cannot cause an implicit replay.
        self._s08_policy_review_ticket=None
        status={}
        try:
            result=await self._host_candidate_request(owner,workspace_id,{'phase':'s02_importance_apply','request':frozen},require_live=True,write_deadline=deadline,before_issue=current,write_status=status)
        except BaseException:
            # Even a pre-ticket failure consumes the original; absence is not
            # an authorization to reconstruct and issue it after uncertainty.
            if self._s08_policy_review_ticket is None:
                self._s08_policy_review_ticket={'s02_importance_consumed':frozen}
            raise
        if (type(result) is not dict or set(result)!={'version','state','revision'} or type(result.get('version')) is not int or result['version']!=1
            or result.get('state') not in ('applied','already_applied') or type(result.get('revision')) is not int
            or result['revision']!=frozen['expected_revision']+1 or self._s08_policy_review_ticket is not status.get('ticket')
            or current() is not True):
            raise MemoryRequestRejectedError('S02 importance outcome unavailable')
        return result

    async def _host_s02(self, *, owner, workspace_id, phase, request=None, current_policy=None, current_account=None, deadline):
        """Private host-only S02 channel; caller retains named-human authority."""
        if phase not in ('capability','override_capability','config','selector','snapshot','apply','status','override_apply','override_status') or any(type(x) is not str or not x or len(x.encode())>512 for x in (owner,workspace_id)):
            raise MemoryRequestRejectedError('S02 unavailable')
        if phase not in ('capability','override_capability') and (type(request) is not dict or request.get('workspace_id')!=workspace_id or (phase not in ('status','override_status') and request.get('owner')!=owner)):
            raise MemoryRequestRejectedError('S02 unavailable')
        payload={'phase':'s02_'+phase,'owner':owner,'workspace_id':workspace_id}
        if request is not None: payload['request']=request
        if phase in ('status','override_status'):
            if type(current_policy) is not dict or type(current_account) is not str or not current_account or request.get('account_id')!=current_account: raise MemoryRequestRejectedError('S02 unavailable')
            payload['account_id']=current_account
            payload['current_policy']=current_policy
        if phase=='apply':
            if not self._host_bounded_write_available(): raise MemoryRequestRejectedError('S02 unavailable')
            result=await self._host_candidate_request(owner,workspace_id,payload,require_live=True,write_deadline=deadline)
        else:
            result=await self._s07_owner_rpc(owner,workspace_id,payload,deadline)
        if type(result) is not dict or result.get('version')!=1 or result.get('state') not in ('ready','applied','already_applied','replayed','recorded','unknown'):
            raise MemoryRequestRejectedError('S02 unavailable')
        return result

    async def _host_metadata_capability(self, *, owner, workspace_id, deadline):
        status=await self._s07_owner_rpc(owner,workspace_id,{'phase':'metadata_assignment_status'},deadline)
        return type(status) is dict and type(status.get('version')) is int and status=={'version':1,'state':'ready','phase':'metadata_assignment_apply'}

    async def _host_metadata_read(self, *, owner,workspace_id,account_id,refs,deadline,curated_id=None):
        if type(refs) is not dict or type(account_id) is not str or not account_id or (curated_id is not None and (type(curated_id) is not str or not curated_id or len(curated_id.encode())>512)):
            raise MemoryRequestRejectedError('metadata unavailable')
        body={'version':1,'owner':owner,'workspace_id':workspace_id,'account_id':account_id,'curated_id':curated_id,'policy_projection':refs}
        phase='metadata_read' if curated_id is not None else 'domain_definition_list'
        result=await self._s07_owner_rpc(owner,workspace_id,{'phase':phase,'request':body},deadline)
        keys={'version','state','curated_id','revision','predecessor_receipt_id','metadata'} if curated_id is not None else {'version','state','items'}
        if type(result) is not dict or set(result)!=keys or type(result.get('version')) is not int or result.get('version')!=1 or result.get('state')!='ready':raise MemoryRequestRejectedError('metadata unavailable')
        if curated_id is not None and (result['curated_id']!=curated_id or type(result['revision']) is not int or result['revision']<1 or type(result['predecessor_receipt_id']) is not str or type(result['metadata']) is not dict):raise MemoryRequestRejectedError('metadata unavailable')
        if curated_id is None and (type(result['items']) is not list or len(result['items'])>128):raise MemoryRequestRejectedError('metadata unavailable')
        return result

    async def _host_metadata_assignment(self, *, owner,workspace_id,account_id,curated_id,predecessor_receipt_id,expected_revision,request_key,reason,metadata,refs,deadline):
        from src.openclank.resource_metadata import validate_metadata
        if (not self._host_bounded_write_available() or type(refs) is not dict or type(deadline) is not float or not math.isfinite(deadline)
            or type(expected_revision) is not int or expected_revision<1 or any(type(x) is not str or not x or x!=x.strip() or len(x.encode())>512 for x in (owner,workspace_id,account_id,curated_id,predecessor_receipt_id,request_key,reason))):
            raise MemoryRequestRejectedError('metadata unavailable')
        try: clean=validate_metadata(metadata)
        except (ValueError,TypeError):raise MemoryRequestRejectedError('metadata unavailable')
        if set(clean)-{'name','aliases','domain_refs'}:raise MemoryRequestRejectedError('metadata unavailable')
        body={'version':1,'owner':owner,'workspace_id':workspace_id,'account_id':account_id,'curated_id':curated_id,'predecessor_receipt_id':predecessor_receipt_id,'expected_revision':expected_revision,'request_key':request_key,'reason':reason,'metadata':clean,'policy_projection':refs}
        result=await self._host_candidate_request(owner,workspace_id,{'phase':'metadata_assignment_apply','request':body},require_live=True,write_deadline=deadline)
        if type(result) is not dict or set(result)!={'state','revision'} or result['state'] not in ('applied','already_applied') or type(result['revision']) is not int or result['revision']!=expected_revision+1:raise MemoryRequestRejectedError('metadata receipt unavailable')
        return result

    async def _host_domain_definition_review(self, *, owner, workspace_id, account_id, request_key, name, aliases, value, reason, refs, deadline):
        if (not self._host_bounded_write_available() or type(refs) is not dict or type(deadline) is not float or not math.isfinite(deadline)
            or any(type(v) is not str or not v or v!=v.strip() or len(v.encode('utf-8'))>512 for v in (owner,workspace_id,account_id,request_key,name,reason))
            or len(name.encode('utf-8'))>256 or len(reason.encode('utf-8'))>500 or type(value) is not str or not value.strip() or len(value.encode('utf-8'))>8192
            or type(aliases) is not list or len(aliases)>32 or any(type(v) is not str or not v or v!=v.strip() or len(v.encode('utf-8'))>256 for v in aliases)
            or len(set(aliases))!=len(aliases)):
            raise MemoryRequestRejectedError('domain review unavailable')
        body={'version':1,'owner':owner,'workspace_id':workspace_id,'account_id':account_id,'request_key':request_key,
              'name':name,'aliases':aliases,'value':value,'reason':reason,'policy_projection':refs}
        result=await self._host_candidate_request(owner,workspace_id,{'phase':'domain_definition_review','request':body},require_live=True,write_deadline=deadline)
        if (type(result) is not dict or set(result)!={'version','state','block_id','revision'} or result.get('version')!=1
            or result.get('state') not in ('applied','already_applied') or type(result.get('revision')) is not int or result['revision']!=1
            or type(result.get('block_id')) is not str or re.fullmatch(r'domain_[0-9a-f]{64}',result['block_id']) is None):
            raise MemoryRequestRejectedError('domain review receipt unavailable')
        return result

    async def _host_account_candidate_apply(self, request, *, account_id, projection, refs, deadline):
        """Host-authorized fixed source-free review; never retries an issued write."""
        if (type(request) is not _HostCandidateReview or request.expected_v2_head!=0 or request.selected_paths!=('/value',)
            or type(account_id) is not str or not account_id or type(projection) is not dict or type(refs) is not dict
            or type(deadline) is not float or not math.isfinite(deadline) or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('account candidate review unavailable')
        body={'version':1,'owner':request.owner,'workspace_id':request.workspace_id,'account_id':account_id,
            'candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,'expected_v2_head':request.expected_v2_head,
            'selected_paths':list(request.selected_paths),'request_key':request.request_key,'reason':request.reason,
            'projection':projection,'embedding':None,'policy_projection':refs}
        result=await self._host_candidate_request(request.owner,request.workspace_id,
            {'phase':'account_candidate_apply','request':body},require_live=True,write_deadline=deadline)
        if (type(result) is not dict or result.get('state')!='applied' or result.get('candidate_id')!=request.candidate_id
            or type(result.get('curated_id')) is not str or not result['curated_id'] or type(result.get('applied_revision')) is not int
            or result['applied_revision']!=1):
            raise MemoryRequestRejectedError('account candidate receipt unavailable')
        if set(result)=={'state','candidate_id','curated_id','applied_revision','visibility'}:
            if result['visibility']!='not_rechecked': raise MemoryRequestRejectedError('account candidate receipt unavailable')
        elif set(result)=={'state','candidate_id','curated_id','applied_revision','applied','field_decision'}:
            if type(result['applied']) is not dict or type(result['field_decision']) is not dict: raise MemoryRequestRejectedError('account candidate receipt unavailable')
        else:
            raise MemoryRequestRejectedError('account candidate receipt unavailable')
        return result

    async def _s07_bounded_candidates(self, *, owner: str, workspace_id: str, invocation, query: str, deadline: float):
        """Optional candidates share the caller's original monotonic deadline."""
        loop=asyncio.get_running_loop(); entered=loop.time()
        from src.memory_provider import RecallInvocation, MemoryRevisionRef
        import math, re
        if type(deadline) is not float or not math.isfinite(deadline): return None
        deadline=min(deadline,entered+0.2)
        if deadline<=entered: return None
        if (type(invocation) is not RecallInvocation or type(owner) is not str or not owner or len(owner)>512 or owner != owner.strip()
                or type(workspace_id) is not str or workspace_id != self._workspace_id or type(query) is not str or len(query)>8192 or not query.strip()
                or type(invocation.execution_revision) is not int or invocation.execution_revision < 1
                or any(type(v) is not str or not v or v != v.strip() or len(v)>512 for v in (invocation.session_id,invocation.root_operation_id,invocation.actor_id,invocation.inner_turn_id,invocation.recall_attempt_id))):
            return None
        try:
            if self._scope(owner).owner != owner or len(query.encode('utf-8'))>8192: return None
        except (Exception, UnicodeError):
            return None
        turn=asdict(invocation)
        started=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s07_recall_start','invocation':turn,'query':query,'limit':20,'budget_ms':1},deadline)
        if (type(started) is not dict or set(started)!={'version','state','token'} or started.get('version')!=1
                or started.get('state')!='running' or type(started.get('token')) is not str or re.fullmatch(r's07_[A-Za-z0-9_-]{1,128}',started['token']) is None):
            return None
        token=started['token']
        while loop.time()<deadline:
            response=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s07_recall_poll','invocation':turn,'token':token},deadline)
            if response is None: return None
            if response == {'version':1,'state':'running'}:
                await asyncio.sleep(min(0.005,max(0,deadline-loop.time()))); continue
            if (set(response)!={'version','state','result'} or response.get('version')!=1 or response.get('state')!='complete'
                    or type(response.get('result')) is not dict or set(response['result'])!={'result','refs'}): return None
            result=response['result']['result']; refs=response['result']['refs']
            if (type(result) is not dict or set(result)!={'results','total'} or type(result['results']) is not list
                    or type(result['total']) is not int or result['total']!=len(result['results']) or len(result['results'])>20
                    or type(refs) is not list or len(refs)!=len(result['results'])): return None
            checked=[]
            for ref in refs:
                if ref is None: checked.append(None); continue
                if (type(ref) is not dict or set(ref)!={'version','provider','owner','workspace_id','project_id','block_id','revision','value_sha256'}
                        or type(ref['version']) is not int or ref['version']!=1 or ref['provider']!='fm-v2' or ref['owner']!=owner or ref['workspace_id']!=workspace_id
                        or ref['project_id'] is not None or type(ref['block_id']) is not str or not ref['block_id']
                        or type(ref['revision']) is not int or ref['revision']<1 or type(ref['value_sha256']) is not str
                        or re.fullmatch(r'[0-9a-f]{64}',ref['value_sha256']) is None): return None
                checked.append(MemoryRevisionRef(**ref))
            from src.openclank.memory_context_admission import FTS_ORDINAL_KEY
            qualified = [(dict(hit,**{FTS_ORDINAL_KEY:i}) if type(hit) is dict else hit,ref) for i,(hit,ref) in enumerate(zip(result['results'],checked)) if ref is not None]
            if loop.time() >= deadline: return None
            return {'results':[hit for hit,_ in qualified], 'total':len(qualified)}, tuple(ref for _,ref in qualified)
        return None

    async def _s07_revalidate_candidates(self, *, owner: str, workspace_id: str, invocation, selected, deadline: float) -> bool:
        """Recheck exact selected IDs and refs; this is not another search."""
        from src.memory_provider import RecallInvocation, MemoryRevisionRef
        import math, re
        loop=asyncio.get_running_loop(); entered=loop.time()
        if type(deadline) is not float or not math.isfinite(deadline): return False
        deadline=min(deadline,entered+0.2)
        if (deadline<=entered or type(invocation) is not RecallInvocation or type(owner) is not str or not owner
                or workspace_id!=self._workspace_id or type(selected) is not tuple or not 1<=len(selected)<=3): return False
        try:
            if self._scope(owner).owner!=owner: return False
            items=[]; ids=set()
            for pair in selected:
                if type(pair) is not tuple or len(pair)!=2: return False
                curated, ref=pair
                if (type(curated) is not str or not curated or curated!=curated.strip() or len(curated)>512 or curated in ids
                        or type(ref) is not MemoryRevisionRef or ref.owner!=owner or ref.workspace_id!=workspace_id or ref.project_id is not None
                        or type(ref.version) is not int or ref.version!=1 or ref.provider!='fm-v2' or type(ref.revision) is not int or ref.revision<1
                        or type(ref.value_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}',ref.value_sha256) is None): return False
                ids.add(curated); items.append({'curated_id':curated,'reference':asdict(ref)})
            turn=asdict(invocation)
        except Exception:
            return False
        started=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s07_revalidate_start','invocation':turn,'selected':items,'budget_ms':1},deadline)
        if (type(started) is not dict or set(started)!={'version','state','token'} or started.get('version')!=1 or started.get('state')!='running'
                or type(started.get('token')) is not str or re.fullmatch(r's07_[A-Za-z0-9_-]{1,128}',started['token']) is None): return False
        while loop.time()<deadline:
            response=await self._s07_owner_rpc(owner,workspace_id,{'phase':'s07_recall_poll','invocation':turn,'token':started['token']},deadline)
            if response is None: return False
            if response=={'version':1,'state':'running'}:
                await asyncio.sleep(min(0.005,max(0,deadline-loop.time())));continue
            return response=={'version':1,'state':'complete','result':{'valid':True}} and loop.time()<deadline
        return False

    async def _host_qualified_search(self, *, owner: str, workspace_id: str, query: str, limit: int):
        """Host-only exact-revision sidecar; never include refs in model output."""
        from src.memory_provider import MemoryRevisionRef
        import re
        if (type(owner) is not str or not owner or owner != owner.strip() or self._scope(owner).owner != owner
            or type(workspace_id) is not str or not workspace_id or workspace_id != self._workspace_id
            or type(query) is not str or not query or len(query.encode('utf-8')) > 8192
            or type(limit) is not int or not 1 <= limit <= 100):
            raise MemoryRequestRejectedError('qualified search unavailable')
        payload = {'phase':'qualified_search','query':query,'limit':limit}
        if len(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')) > 16384:
            raise MemoryRequestRejectedError('qualified search unavailable')
        response = await self._host_candidate_request(owner,workspace_id,payload,require_live=True)
        if (type(response) is not dict or type(response.get('result')) is not dict
            or set(response['result']) != {'results','total'} or type(response['result']['results']) is not list
            or type(response['result']['total']) is not int or response['result']['total'] < len(response['result']['results'])
            or len(response['result']['results']) > limit):
            raise MemoryRequestRejectedError('qualified search response unavailable')
        if (set(response) != {'result','refs'} or type(response.get('refs')) is not list
                or len(response['refs']) != len(response['result']['results'])):
            return response['result'], tuple(None for _ in response['result']['results'])
        refs = []
        for item in response['refs']:
            if item is None:
                refs.append(None); continue
            if (type(item) is not dict or set(item) != {'version','provider','owner','workspace_id','project_id','block_id','revision','value_sha256'}
                or type(item['version']) is not int or item['version'] != 1 or item['provider'] != 'fm-v2'
                or item['owner'] != owner or item['workspace_id'] != workspace_id or item['project_id'] is not None
                or type(item['block_id']) is not str or not item['block_id'] or item['block_id'] != item['block_id'].strip() or len(item['block_id']) > 512
                or type(item['revision']) is not int or item['revision'] < 1 or type(item['value_sha256']) is not str
                or re.fullmatch(r'[0-9a-f]{64}',item['value_sha256']) is None):
                return response['result'], tuple(None for _ in response['result']['results'])
            refs.append(MemoryRevisionRef(**item))
        return response['result'], tuple(refs)

    async def _host_mirror_context(self, request: _HostMirrorContext) -> Dict[str, Any]:
        if type(request) is not _HostMirrorContext:
            raise MemoryRequestRejectedError("host-private mirror context unavailable")
        if request.account_id is not None and (type(request.account_id) is not str or not request.account_id or request.account_id != request.account_id.strip() or len(request.account_id)>512):
            raise MemoryRequestRejectedError("host-private mirror account unavailable")
        payload={"phase":"mirror_context", "curated_id":request.memory_id}
        if request.account_id is not None: payload['account_id']=request.account_id
        result = await self._host_candidate_request(request.owner, request.workspace_id, payload)
        binding = result.get("binding")
        if (result.get("mode") != "guarded" or not isinstance(binding, dict) or
            binding.get("version") != "s03.curated-mirror.v1" or binding.get("owner") != request.owner or
            binding.get("workspace") != request.workspace_id or binding.get("project") != "" or
            binding.get("curated_id") != request.memory_id or type(binding.get("revision")) is not int or
            result.get("expected_revision") != binding["revision"] or
            not all(isinstance(result.get(k), str) and result[k] for k in ("candidate_id","origin_request_key","prior_receipt_id"))):
            raise MemoryRequestRejectedError("invalid mirror context")
        # Account provenance is an internal prepared-read check, not UI data.
        return {key:value for key,value in result.items() if key != 'reviewer_account_id'}

    @staticmethod
    def _validate_host_mirror_operation(request: _HostMirrorApply) -> Dict[str, Any]:
        import hashlib

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate operation key")
                result[key] = value
            return result

        try:
            if hashlib.sha256(request.operation_json.encode("utf-8")).hexdigest() != request.operation_sha256:
                raise ValueError("digest mismatch")
            operation = json.loads(request.operation_json, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
        except (ValueError, TypeError, UnicodeError) as exc:
            raise MemoryRequestRejectedError("invalid sealed mirror operation") from exc
        keys = {"version", "owner", "workspace", "project", "block_id", "expected_revision", "payload", "request_key", "actor_type", "actor_id", "reason", "source_event_id", "approved_paths", "producer"}
        if (not isinstance(operation, dict) or set(operation) != keys or
            operation["version"] != "s03.published-update.v1" or operation["owner"] != request.owner or
            operation["workspace"] != request.workspace_id or operation["project"] != "" or
            operation["expected_revision"] != request.expected_revision or type(operation["expected_revision"]) is not int or
            operation["request_key"] != request.request_key or operation["actor_type"] != "user" or
            operation["actor_id"] != request.owner or operation["reason"] != request.reason or
            operation["source_event_id"] is not None or operation["producer"] is not None or
            operation["approved_paths"] != list(request.selected_paths) or
            not isinstance(operation["block_id"], str) or not operation["block_id"] or
            not isinstance(operation["payload"], dict)):
            raise MemoryRequestRejectedError("sealed mirror operation context mismatch")
        return operation

    async def _host_mirror_preview(self, request: _HostMirrorPreview) -> Dict[str, Any]:
        if type(request) is not _HostMirrorPreview:
            raise MemoryRequestRejectedError("host-private mirror review unavailable")
        preview = await self._host_candidate_request(request.owner, request.workspace_id, {
            "phase":"mirror_preview", "candidate_id":request.candidate_id,
            "origin_request_key":request.origin_request_key, "prior_receipt_id":request.prior_receipt_id,
            "expected_revision":request.expected_revision, "request_key":request.request_key,
            "reason":request.reason, "selected_paths":list(request.selected_paths), "payload":request.payload,
        })
        binding = preview.get("binding")
        if (preview.get("version") != "s03.mirror-preview.v1" or not isinstance(binding, dict) or
            binding.get("owner") != request.owner or binding.get("workspace") != request.workspace_id or
            binding.get("project") != "" or binding.get("curated_id") != request.memory_id or
            binding.get("revision") != request.expected_revision or preview.get("expected_revision") != request.expected_revision or
            preview.get("selected_paths") != list(request.selected_paths) or
            not isinstance(preview.get("changes"), list) or not isinstance(preview.get("normalized_payload"), dict) or
            not isinstance(preview.get("operation_json"), str) or not isinstance(preview.get("operation_sha256"), str)):
            raise MemoryRequestRejectedError("invalid mirror preview")
        check = _HostMirrorApply(request.owner,request.workspace_id,request.memory_id,request.candidate_id,
            request.origin_request_key,request.prior_receipt_id,request.expected_revision,request.request_key,
            request.reason,request.selected_paths,preview["operation_json"],preview["operation_sha256"])
        operation = self._validate_host_mirror_operation(check)
        if operation["block_id"] != binding.get("block_id") or operation["payload"] != preview["normalized_payload"]:
            raise MemoryRequestRejectedError("invalid mirror preview binding")
        return preview

    async def _host_mirror_apply(self, request: _HostMirrorApply, *, before_issue) -> Dict[str, Any]:
        if type(request) is not _HostMirrorApply or not callable(before_issue):
            raise MemoryRequestRejectedError("host-private mirror review unavailable")
        # One in-process original owns issue and any uncertain outcome. The
        # lock is acquired before the first Native read or write.
        async with self._mirror_review_lock:
            operation = self._validate_host_mirror_operation(request)
            original = self._mirror_review_original
            if original is not None and original != request:
                raise MemoryRequestRejectedError("mirror original requires reconciliation")
            reconciling = original is not None
            if before_issue() is not True:
                raise MemoryRequestRejectedError("mirror review authority unavailable")
            base = {"candidate_id":request.candidate_id, "origin_request_key":request.origin_request_key,
                    "prior_receipt_id":request.prior_receipt_id, "expected_revision":request.expected_revision,
                    "request_key":request.request_key, "operation_json":request.operation_json}
            replay = await self._host_candidate_request(request.owner,request.workspace_id,{"phase":"mirror_replay", **base})
            if replay.get("state") == "applied":
                self._validate_host_mirror_result(request, operation, replay)
                if before_issue() is not True:
                    raise MemoryRequestRejectedError("mirror review authority unavailable")
                self._mirror_review_original = None
                return replay
            if replay.get("state") != "needs_preview":
                raise MemoryRequestRejectedError("mirror replay unavailable")
            preview = await self._host_mirror_preview(_HostMirrorPreview(request.owner,request.workspace_id,request.memory_id,
                request.candidate_id,request.origin_request_key,request.prior_receipt_id,request.expected_revision,
                request.request_key,request.reason,request.selected_paths,operation["payload"]))
            if preview["operation_json"] != request.operation_json or preview["operation_sha256"] != request.operation_sha256:
                raise MemoryRequestRejectedError("mirror preview changed")
            if before_issue() is not True:
                raise MemoryRequestRejectedError("mirror review authority unavailable")
            if reconciling:
                # Exact current lineage/head plus drained original proves this
                # DB-only operation did not survive; another Save is required.
                self._mirror_review_original = None
                return {"state":"needs_review"}
            status = {}
            self._mirror_review_original = request
            try:
                result = await self._host_candidate_request(request.owner,request.workspace_id,{"phase":"mirror_apply", **base,
                    "reason":request.reason, "selected_paths":list(request.selected_paths), "operation_sha256":request.operation_sha256},
                    require_live=True, write_deadline=asyncio.get_running_loop().time()+5.0,
                    before_issue=before_issue, write_status=status)
                self._validate_host_mirror_result(request, operation, result)
                self._mirror_review_original = None
                return result
            except BaseException:
                ticket = status.get('ticket')
                if ticket is None or ticket.get('issued') is not True:
                    self._mirror_review_original = None
                raise

    @staticmethod
    def _validate_host_mirror_result(request: _HostMirrorApply, operation: Dict[str, Any], result: Dict[str, Any]) -> None:
        binding, applied = result.get("binding"), result.get("applied")
        if (result.get("state") != "applied" or not isinstance(result.get("receipt_id"), str) or not result["receipt_id"] or
            not isinstance(binding, dict) or set(binding) != {"version", "owner", "workspace", "project", "curated_id", "block_id", "revision", "guarded_change_set_id", "semantic_sha256", "field_map"} or binding.get("version") != "s03.curated-mirror.v1" or
            binding.get("owner") != request.owner or binding.get("workspace") != request.workspace_id or
            binding.get("project") != "" or binding.get("curated_id") != request.memory_id or
            binding.get("block_id") != operation["block_id"] or type(binding.get("revision")) is not int or
            binding["revision"] != request.expected_revision + 1 or not isinstance(binding.get("guarded_change_set_id"), str) or
            not isinstance(binding.get("semantic_sha256"), str) or len(binding["semantic_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in binding["semantic_sha256"]) or
            not isinstance(binding.get("field_map"), dict) or binding["field_map"].get("content") != "/value" or
            any((key, value) not in {("content", "/value"), ("confidence_score", "/confidence"), ("trust_score", "/trust"), ("tags", "/tags")} for key, value in binding["field_map"].items()) or
            not isinstance(applied, dict) or applied.get("revision") != binding["revision"] or
            applied.get("change_set_id") != binding["guarded_change_set_id"] or not isinstance(result.get("field_decision"), dict)):
            raise MemoryRequestRejectedError("mirror result binding mismatch")
        if "/activation_rationale/supersedes" in request.selected_paths:
            # A relation lives in the revision payload, not in the curated mapping: it is never in binding.field_map.
            # The decision must name it as an applied path.
            allowed = result["field_decision"].get("allowed_paths")
            if not isinstance(allowed, list) or "/activation_rationale/supersedes" not in allowed:
                raise MemoryRequestRejectedError("mirror result binding mismatch")

    async def _host_append_derived_compaction(self, *, ready, value, lifecycle, source_owner, guard, policy_projection):
        """Fixed host-only native artifact write; caller retains source guard."""
        from src.memery_external_sources import SourceRef, _source_ref_error
        from src.openclank.chat_source_bridge import CompletedTurnLifecycle, CompletedTurnLifecycleOwner
        from src.openclank.conversation_archive import TrustedMaintenanceReady
        from src.openclank.memory_job_admission import _conn
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if (type(ready) is not TrustedMaintenanceReady or type(lifecycle) is not CompletedTurnLifecycle or type(value) is not dict
                or type(source_owner) is not CompletedTurnLifecycleOwner or type(policy_projection) is not PolicyProjection
                or policy_projection._preparation is not self._guarded_preparation):
            raise MemoryRequestRejectedError('maintenance authority unavailable')
        payload=ready.payload();proof=payload.get('proof');descriptor=payload.get('descriptor');identity=payload.get('identity')
        if type(proof) is not dict or type(descriptor) is not dict or type(identity) is not dict:
            raise MemoryRequestRejectedError('maintenance authority unavailable')
        try:
            source=SourceRef(**proof['source'])
            parts=value['parts']
            if (_provider_source_ref_error(self, source) or source!=lifecycle.source or self is not lifecycle.provider
                or value.get('state')!='ready' or value.get('sessionID')!=descriptor['sessionID']
                or value.get('summaryMessageID')!=descriptor['summaryMessageID']
                or value.get('providerID')!=descriptor['providerID'] or value.get('modelID')!=descriptor['modelID']
                or type(parts) is not list or not 1<=len(parts)<=16
                or any(type(p) is not dict or set(p)!={'partID','text','sha256'} or type(p['text']) is not str
                       or hashlib.sha256(p['text'].encode('utf-8')).hexdigest()!=p['sha256'] for p in parts)
                or sum(len(p['text'].encode('utf-8')) for p in parts)>65536
                or [{'partID':p['partID'],'sha256':p['sha256']} for p in parts]!=descriptor['parts']):
                raise ValueError('maintenance artifact changed')
            lifecycle.assert_current(source=source,provider=self,profile=lifecycle.profile,target_authority=lifecycle.target_authority,guard=guard)
            with _conn(self._local_db_path()) as db:
                if not self._maintenance_capability(db): raise ValueError('maintenance capability unavailable')
            owner=lifecycle.binding.memory_owner;workspace=lifecycle.binding.memory_workspace
            coordinator=policy_coordinator()
            if coordinator is None or coordinator.projection is not policy_projection:
                raise ValueError('maintenance policy unavailable')
            refs=coordinator.checked_refs(source_owner.owner,source.account_id,workspace)
            request={'version':1,'owner':owner,'workspace_id':workspace,'source':asdict(source),
                'native_session_id':identity['sessionID'],'summary_message_id':descriptor['summaryMessageID'],
                'producer_provider_id':descriptor['providerID'],'producer_model_id':descriptor['modelID'],
                'root_operation_id':identity['rootOperationID'],'actor_id':identity['actorID'],
                'execution_revision':identity['executionRevision'],'inner_turn_id':identity['innerTurnID'],
                'source_attestation_sha256':proof['sourceAttestationSHA256'],'source_message_id':proof['sourceMessageID'],
                'source_part_id':proof['sourcePartID'],'source_text_sha256':proof['sourceTextSHA256'],
                'dispatch_id':proof['dispatchID'],'body_sha256':proof['bodySHA256'],'prompt_sha256':proof['promptSHA256'],
                'wire_profile':proof['wireProfile'],'instructions_sha256':proof.get('instructionsSHA256'),
                'instructions_class':proof.get('instructionsClass'),
                'parts':[{'part_id':p['partID'],'text':p['text'],'sha256':p['sha256']} for p in parts],
                'policy_projection':refs}
            source_bytes=json.dumps([source.version,source.account_id,source.chat_id,source.actor_id,source.message_id,source.part_id],ensure_ascii=False,separators=(',',':')).encode()
            source_id='hostpart_'+hashlib.sha256(source_bytes).hexdigest()
            key=json.dumps([source.account_id,source_id,identity['sessionID'],descriptor['summaryMessageID']],ensure_ascii=False,separators=(',',':')).encode()
            expected='derived_compaction_'+hashlib.sha256(key).hexdigest()
        except (KeyError,TypeError,ValueError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('maintenance artifact unavailable') from exc
        current=await asyncio.to_thread(source_owner.for_source,source=source,guard=guard)
        if current != lifecycle:
            raise MemoryRequestRejectedError('maintenance source changed')
        result=await self._host_candidate_request(owner,workspace,{'phase':'derived_compaction','request':request},require_live=True,
            write_deadline=float(asyncio.get_running_loop().time()+30.0))
        lifecycle.assert_current(source=source,provider=self,profile=lifecycle.profile,target_authority=lifecycle.target_authority,guard=guard)
        if type(result.get('version')) is not int or result!={'version':1,'evidence_id':expected}:
            raise MemoryRequestRejectedError('maintenance receipt unavailable')
        return result

    async def _host_s08_review_preview(self, request: _HostCandidatePreview):
        """Resolve bounded contributor references; caller authorizes each under its guard."""
        from src.memery_external_sources import SourceRef, _source_ref_error
        if type(request) is not _HostCandidatePreview or not request.candidate_id.startswith('s08candidate_'):
            raise MemoryRequestRejectedError('S08 review unavailable')
        result = await self._host_candidate_request(request.owner,request.workspace_id,
            {'phase':'s08_review_preview','candidate_id':request.candidate_id},require_live=True)
        if (set(result) != {'candidate_id','candidate_digest','sources'} or result['candidate_id'] != request.candidate_id
            or not isinstance(result['candidate_digest'],str) or re.fullmatch(r'[0-9a-f]{64}',result['candidate_digest']) is None
            or type(result['sources']) is not list or not 1 <= len(result['sources']) <= 64):
            raise MemoryRequestRejectedError('S08 review unavailable')
        try:
            sources=tuple(SourceRef(**item) for item in result['sources'])
        except (TypeError,ValueError):
            raise MemoryRequestRejectedError('S08 review unavailable')
        if any(_provider_source_ref_error(self, source) for source in sources) or len(set(sources)) != len(sources):
            raise MemoryRequestRejectedError('S08 review unavailable')
        return result['candidate_digest'], sources

    async def _host_s08_review_apply(self, request: _HostCandidateReview, *, account, policy_projection, refs):
        """Apply only the exact preview after host authorization of every source."""
        from src.openclank.memory_policy_projection import PolicyProjection, policy_coordinator
        if type(request) is not _HostCandidateReview or request.expected_v2_head != 0 or request.selected_paths != ('/value',):
            raise MemoryRequestRejectedError('S08 review unavailable')
        coordinator=policy_coordinator()
        if (type(policy_projection) is not PolicyProjection or coordinator is None
                or coordinator.projection is not policy_projection or policy_projection._preparation is not self._guarded_preparation
                or coordinator.checked_refs(request.owner,account,request.workspace_id)!=refs):
            raise MemoryRequestRejectedError('S08 review policy unavailable')
        result=await self._host_candidate_request(request.owner,request.workspace_id,
            {'phase':'s08_review_apply','candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,
             'request_key':request.request_key,'reason':request.reason,'policy_projection':refs},require_live=True)
        if (set(result) != {'state','candidate_id','block_id','curated_id','revision','visibility'}
            or result['state'] != 'applied' or result['candidate_id'] != request.candidate_id
            or type(result['block_id']) is not str or type(result['curated_id']) is not str
            or type(result['revision']) is not int or result['revision'] != 1 or result['visibility'] not in ('current','not_rechecked')):
            raise MemoryRequestRejectedError('S08 review unavailable')
        return result

    async def _host_s08_review_status(self, request: _HostCandidateReview):
        if type(request) is not _HostCandidateReview or request.expected_v2_head != 0 or request.selected_paths != ('/value',):
            raise MemoryRequestRejectedError('S08 review unavailable')
        result=await self._host_candidate_request(request.owner,request.workspace_id,
            {'phase':'s08_review_status','candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,
             'request_key':request.request_key,'reason':request.reason},require_live=True)
        if result == {'state':'needs_preview'}:
            return result
        if (set(result) != {'state','candidate_id','block_id','curated_id','revision','visibility'}
            or result['state'] != 'applied' or result['candidate_id'] != request.candidate_id
            or type(result['block_id']) is not str or type(result['curated_id']) is not str
            or type(result['revision']) is not int or result['revision'] != 1 or result['visibility'] != 'not_rechecked'):
            raise MemoryRequestRejectedError('S08 review unavailable')
        return result

    async def _host_candidate_source(self, request: _HostCandidatePreview):
        """Resolve the stored source reference over the existing private channel."""
        if type(request) is not _HostCandidatePreview:
            raise MemoryRequestRejectedError('host-private source unavailable')
        result = await self._host_candidate_request(request.owner,request.workspace_id,{'phase':'candidate_source','candidate_id':request.candidate_id})
        if set(result) != {'candidate_id','source_ref'} or result['candidate_id'] != request.candidate_id:
            raise MemoryRequestRejectedError('candidate source unavailable')
        if result['source_ref'] is None:
            return None
        from src.memery_external_sources import SourceRef, _source_ref_error
        try:
            source = SourceRef(**result['source_ref'])
        except (TypeError,ValueError):
            raise MemoryRequestRejectedError('candidate source unavailable')
        if _provider_source_ref_error(self, source):
            raise MemoryRequestRejectedError('candidate source unavailable')
        return source

    async def _host_candidate_receipt_status(self, request: _HostCandidateReview) -> Dict[str, Any]:
        """Content-free exact retry status; never implies current source visibility."""
        if type(request) is not _HostCandidateReview:
            raise MemoryRequestRejectedError('candidate receipt unavailable')
        result = await self._host_candidate_request(request.owner,request.workspace_id,{
            'phase':'receipt_status','candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,
            'expected_v2_head':request.expected_v2_head,'selected_paths':list(request.selected_paths),
            'request_key':request.request_key,'reason':request.reason})
        if type(result) is not dict:
            raise MemoryRequestRejectedError('candidate receipt unavailable')
        if result == {'state':'needs_preview'}:
            return result
        if (set(result) != {'state','candidate_id','curated_id','applied_revision','visibility'} or result['state'] != 'applied'
            or result['candidate_id'] != request.candidate_id or type(result['curated_id']) is not str
            or not result['curated_id'] or result['curated_id'] != result['curated_id'].strip() or len(result['curated_id']) > 512
            or type(result['applied_revision']) is not int or result['applied_revision'] < 1 or result['visibility'] != 'not_rechecked'):
            raise MemoryRequestRejectedError('candidate receipt unavailable')
        return result

    async def _host_candidate_replay(self, request: _HostCandidateReview) -> Dict[str, Any]:
        if type(request) is not _HostCandidateReview:
            raise MemoryRequestRejectedError('host-private review unavailable')
        result = await self._host_candidate_request(request.owner,request.workspace_id,{
            'phase':'replay','candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,
            'expected_v2_head':request.expected_v2_head,'selected_paths':list(request.selected_paths),
            'request_key':request.request_key,'reason':request.reason})
        if result.get('state') not in ('needs_preview','applied'):
            raise MemoryRequestRejectedError('candidate receipt unavailable')
        if result['state']=='applied' and (result.get('candidate_id')!=request.candidate_id or not isinstance(result.get('curated_id'),str)
            or not isinstance(result.get('applied'),dict) or type(result.get('applied_revision')) is not int or not isinstance(result.get('field_decision'),dict)):
            raise MemoryRequestRejectedError('invalid candidate receipt')
        return result

    async def _host_candidate_prepare_embedding(self, request: _HostCandidateReview, *, admission):
        import math
        if (type(request) is not _HostCandidateReview or type(admission) is not _HostCandidateEmbeddingAdmission
            or admission._seal is not _CANDIDATE_EMBEDDING_ADMISSION_SEAL or admission.used
            or admission.provider is not self or admission.request != request):
            raise MemoryRequestRejectedError('candidate embedding admission unavailable')
        admission.used = True
        result = await self._host_candidate_request(request.owner,request.workspace_id,{
            'phase':'candidate_embedding','candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest})
        if (set(result)!={'state','candidate_id','candidate_digest','embedding'} or result['state']!='prepared'
            or result['candidate_id']!=request.candidate_id or result['candidate_digest']!=request.candidate_digest):
            raise MemoryRequestRejectedError('candidate embedding unavailable')
        vector=result['embedding']
        if vector is not None and (not isinstance(vector,list) or len(vector)>8192 or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector)):
            raise MemoryRequestRejectedError('candidate embedding unavailable')
        return vector

    async def _host_candidate_apply_prepared(self, request: _HostCandidateReview, projection: Dict[str, Any], embedding, *, source, guard) -> Dict[str, Any]:
        if type(request) is not _HostCandidateReview or not isinstance(projection,dict):
            raise MemoryRequestRejectedError('host-private review unavailable')
        _validate_candidate_source_guard(self,request,source,guard)
        result = await self._host_candidate_request(request.owner,request.workspace_id,{
            'phase':'candidate_apply_prepared','candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,
            'expected_v2_head':request.expected_v2_head,'selected_paths':list(request.selected_paths),
            'request_key':request.request_key,'reason':request.reason,'projection':projection,'embedding':embedding})
        if (result.get('state')!='applied' or result.get('candidate_id')!=request.candidate_id
            or not isinstance(result.get('curated_id'),str) or not isinstance(result.get('applied'),dict)
            or type(result.get('applied_revision')) is not int or not isinstance(result.get('field_decision'),dict)):
            raise MemoryRequestRejectedError('invalid candidate receipt')
        return result

    async def _host_candidate_reject_preview(self, request: _HostCandidatePreview) -> Dict[str, Any]:
        if type(request) is not _HostCandidatePreview:
            raise MemoryRequestRejectedError('host-private rejection unavailable')
        result = await self._host_candidate_request(request.owner,request.workspace_id,{
            'phase':'reject_preview','candidate_id':request.candidate_id})
        if (set(result) != {'state','candidate_id','candidate_digest','association_sha256','source_account_id','source_chat_id','source_ref'}
            or result['state'] != 'preview' or result['candidate_id'] != request.candidate_id
            or not all(isinstance(result[key],str) and result[key] for key in ('source_account_id','source_chat_id'))
            or any(not isinstance(result[key],str) or re.fullmatch(r'[0-9a-f]{64}',result[key]) is None for key in ('candidate_digest','association_sha256'))):
            raise MemoryRequestRejectedError('invalid candidate rejection preview')
        from src.memery_external_sources import SourceRef, _source_ref_error
        try:
            source_ref = SourceRef(**result['source_ref'])
        except (TypeError, ValueError):
            raise MemoryRequestRejectedError('invalid candidate source reference')
        if (_provider_source_ref_error(self, source_ref) or source_ref.account_id != result['source_account_id']
            or source_ref.chat_id != result['source_chat_id']):
            raise MemoryRequestRejectedError('invalid candidate source reference')
        return result

    async def _host_candidate_reject(self, request: _HostCandidateRejection) -> Dict[str, Any]:
        if type(request) is not _HostCandidateRejection:
            raise MemoryRequestRejectedError('host-private rejection unavailable')
        result = await self._host_candidate_request(request.owner,request.workspace_id,{
            'phase':'reject_apply','candidate_id':request.candidate_id,'candidate_digest':request.candidate_digest,
            'association_sha256':request.association_sha256,'source_account_id':request.source_account_id,'source_chat_id':request.source_chat_id,
            'request_key':request.request_key,'reason':request.reason})
        if (set(result) != {'state','candidate_id','request_key','replayed'} or result['state'] != 'rejected'
            or result['candidate_id'] != request.candidate_id or result['request_key'] != request.request_key
            or type(result['replayed']) is not bool):
            raise MemoryRequestRejectedError('invalid candidate rejection receipt')
        return result

    async def _host_candidate_preview(self, request: _HostCandidatePreview) -> Dict[str, Any]:
        if type(request) is not _HostCandidatePreview:
            raise MemoryRequestRejectedError("host-private review is unavailable")
        preview = await self._host_candidate_request(request.owner, request.workspace_id, {
            "phase":"preview", "candidate_id":request.candidate_id, "reason":request.reason,
        })
        candidate, record = preview.get("candidate"), preview.get("curated_record")
        digest = preview.get("snapshot_digest")
        snapshot_fields = {"id", "content", "kind", "confidence_score", "importance_score", "owner", "workspace_id",
            "workspace_path", "session_id", "turn_id", "raw_evidence_ids", "evidence_role", "source", "source_event_id",
            "dedup_key", "source_uri", "source_revision", "content_hash", "source_message_ids", "status", "reason",
            "accepted_curated_id", "created_at", "updated_at"}
        if (preview.get("snapshot_version") != "s03.candidate-snapshot.v1" or
            not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or
            not isinstance(candidate, dict) or set(candidate) != snapshot_fields or not isinstance(record, dict) or
            candidate.get("id") != request.candidate_id or candidate.get("owner") != request.owner or
            candidate.get("workspace_id") != request.workspace_id or candidate.get("status") != "pending" or
            record.get("owner") != request.owner or record.get("workspace_id") != request.workspace_id or
            record.get("content") != candidate.get("content") or record.get("kind") != candidate.get("kind") or
            record.get("source") != candidate.get("source") or record.get("source_type") != "human" or
            record.get("confidence_score") != candidate.get("confidence_score") or
            record.get("importance_score") != candidate.get("importance_score") or
            record.get("session_id") != candidate.get("session_id") or
            record.get("source_message_ids") != candidate.get("source_message_ids") or
            not isinstance(record.get("metadata"), dict) or record["metadata"].get("candidate_id") != request.candidate_id or
            record["metadata"].get("admission_reason") != request.reason or
            not isinstance(record.get("id"), str) or not record["id"]):
            raise MemoryRequestRejectedError("invalid host-private candidate response")
        from src.memery_v2 import prepare_mirror_projection
        projection = prepare_mirror_projection(self._record(record), owner=request.owner, workspace_id=request.workspace_id,
            action="review_candidate", db_path=self._fm_db_path, strict=True)
        if projection is None:
            raise MemoryRequestRejectedError("candidate projection unavailable")
        import sqlite3
        from contextlib import closing
        from urllib.parse import quote
        uri = "file:" + quote(os.path.abspath(self._fm_db_path), safe="/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            row = conn.execute("SELECT workspace_key,project_key,current_revision FROM fm_v2_knowledge_blocks WHERE owner_id=? AND block_id=?", (request.owner,projection["block_id"])).fetchone()
        if row and (row[0], row[1]) != (request.workspace_id, ""):
            raise MemoryRequestRejectedError("candidate projection unavailable")
        return {"snapshot_version":preview["snapshot_version"], "candidate_digest":digest,
                "expected_v2_head":int(row[2]) if row else 0, "selected_paths":["/value"],
                "candidate":candidate, "curated_id":record["id"], "projection":projection}

    async def _host_candidate_review(self, request: _HostCandidateReview) -> Dict[str, Any]:
        """Private app-owned channel; acceptance is intentionally unavailable."""
        if type(request) is not _HostCandidateReview:
            raise MemoryRequestRejectedError("host-private review is unavailable")
        receipt = await self._host_candidate_request(request.owner, request.workspace_id, {
            "phase":"replay", "candidate_id":request.candidate_id, "candidate_digest":request.candidate_digest,
            "expected_v2_head":request.expected_v2_head, "selected_paths":list(request.selected_paths),
            "request_key":request.request_key, "reason":request.reason,
        })
        if receipt.get("state") == "applied":
            if (receipt.get("candidate_id") != request.candidate_id or not isinstance(receipt.get("curated_id"), str) or
                not isinstance(receipt.get("applied"), dict) or type(receipt.get("applied_revision")) is not int or
                not isinstance(receipt.get("field_decision"), dict)):
                raise MemoryRequestRejectedError("invalid candidate receipt response")
            return receipt
        if receipt.get("state") != "needs_preview":
            raise MemoryRequestRejectedError("invalid candidate receipt response")
        preview = await self._host_candidate_preview(_HostCandidatePreview(
            request.owner, request.workspace_id, request.candidate_id, request.reason))
        if (preview["candidate_digest"] != request.candidate_digest or
            preview["expected_v2_head"] != request.expected_v2_head or
            list(request.selected_paths) != preview["selected_paths"]):
            raise MemoryRequestRejectedError("candidate review snapshot is stale")
        outcome = await self._host_candidate_request(request.owner, request.workspace_id, {
            "phase":"apply", "candidate_id":request.candidate_id, "candidate_digest":request.candidate_digest,
            "expected_v2_head":request.expected_v2_head, "selected_paths":list(request.selected_paths),
            "request_key":request.request_key, "reason":request.reason, "projection":preview["projection"],
        })
        if (outcome.get("state") != "applied" or outcome.get("candidate_id") != request.candidate_id or
            not isinstance(outcome.get("curated_id"), str) or not isinstance(outcome.get("applied"), dict) or
            type(outcome.get("applied_revision")) is not int or not isinstance(outcome.get("field_decision"), dict)):
            raise MemoryRequestRejectedError("invalid guarded candidate result")
        return outcome

    async def review_candidate(
        self,
        candidate_id: str,
        *,
        accept: bool,
        reason: str,
        owner: str,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        result = await self._call_tool("review_candidate", {
            "id": candidate_id,
            "accept": accept,
            "reason": reason,
            "owner": scope.owner,
            "workspace_id": workspace_id or scope.workspace_id,
        })
        curated_id = result.get("curated_id") if accept else None
        if curated_id and self._local_db_path() is not None:
            try:
                record = await self._get_legacy_current(
                    str(curated_id),
                    owner=owner,
                    workspace_id=workspace_id or scope.workspace_id,
                )
                if record is not None:
                    from src.memery_v2 import mirror_record
                    mirror_record(
                        record,
                        owner=scope.owner,
                        workspace_id=workspace_id or scope.workspace_id,
                        action="review_candidate",
                        db_path=self._fm_db_path,
                    )
            except Exception:
                logger.debug("v2 candidate review mirror unavailable", exc_info=True)
        return result

    async def update_candidate(
        self,
        candidate_id: str,
        *,
        text: str,
        category: Optional[str] = None,
        reason: str = "edited_by_user",
        owner: str,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Edit one exact-tenant pending Rust candidate without activation."""
        scope = self._scope(owner)
        result = await self._call_tool(
            "update_candidate",
            {
                "id": candidate_id,
                "content": str(text),
                "category": category,
                "reason": str(reason or "edited_by_user")[:500],
                "owner": scope.owner,
                "workspace_id": workspace_id or scope.workspace_id,
            },
        )
        candidate = result.get("candidate")
        if not result.get("updated") or not isinstance(candidate, dict):
            raise MemoryRequestRejectedError("pending candidate edit was rejected")
        return candidate

    async def handler_display_label(self, *, owner: Optional[str] = None) -> str:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            result = await self._call_tool("handler_display_label", {
                "owner": scope.owner, "workspace_id": scope.workspace_id,
            })
            return str(result.get("label") or "Handler")
        from services.memory.principal_context import resolve_handler_display_label
        return await asyncio.to_thread(resolve_handler_display_label, scope.owner,
                                       workspace_id=scope.workspace_id, db_path=local_db)

    async def mark_conversation_source_removed(self, *, owner: str, session_id: str) -> Dict[str, Any]:
        """Detach one erased chat's provenance without erasing independent content."""
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            return await self._call_tool("mark_conversation_source_removed", {
                "owner": scope.owner, "session_id": session_id,
                "workspace_id": scope.workspace_id,
            })
        from src.memery_v2 import mark_conversation_source_removed, conversation_source_workspaces
        bindings = await asyncio.to_thread(conversation_source_workspaces,
            owner=scope.owner, session_id=session_id, db_path=local_db)
        receipts = []
        for workspace in bindings:
            receipts.append(await asyncio.to_thread(mark_conversation_source_removed,
                owner=scope.owner, session_id=session_id, workspace_id=workspace, db_path=local_db))
        return {
            "complete": all(receipt.get("complete") is True for receipt in receipts),
            "owner": scope.owner, "session_id": session_id,
            "workspace_bindings": bindings, "receipts": receipts,
            "detached": {key: sum(int(receipt.get("detached", {}).get(key, 0)) for receipt in receipts) for key in ("curated", "raw", "candidates")},
            "independent_content_retained": True,
            "already_applied": bool(receipts) and all(receipt.get("already_applied") for receipt in receipts),
        }

    async def versioned_detail(
        self,
        memory_id: str,
        *,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        if self._local_db_path() is None:
            return await self._call_tool("versioned_detail", {"memory_id": memory_id, "owner": scope.owner, "workspace_id": scope.workspace_id})
        from src.memory_versioned import get_knowledge

        def read():
            detail = get_knowledge(memory_id, owner=scope.owner, workspace_id=scope.workspace_id, db_path=self._fm_db_path)
            relation = self._stored_relation((detail.get("current") or {}).get("activation_rationale"))
            if relation is not None:
                detail["supersedes"] = self._relation_view(scope, relation)
            return detail

        return await asyncio.to_thread(read)

    @staticmethod
    def _stored_relation(rationale):
        """The closed supersedes object of a revision's activation_rationale, or None."""
        relation = rationale.get("supersedes") if isinstance(rationale, dict) else None
        if (isinstance(relation, dict) and set(relation) == {"block_id", "revision"} and isinstance(relation["block_id"], str)
                and relation["block_id"] and type(relation["revision"]) is int and relation["revision"] > 0):
            return relation
        return None

    def _relation_view(self, scope, relation):
        """Derived read field. A target that cannot be resolved for this owner/workspace (erased, reset) is reported only as
        target_available false; target_id (the target's memory id, for opening it) is given only when it resolves."""
        view = {"block_id": relation["block_id"], "revision": relation["revision"], "target_available": False}
        try:
            target = self._versioned_read(relation["block_id"], scope.owner, lambda repo, sc, block: repo.get_knowledge(
                owner=sc.owner, block_id=block, workspace_id=sc.workspace_id))
        except Exception:
            return view
        view["target_available"] = True
        view["target_id"] = target.get("claim_slot")
        return view

    async def versioned_relation(self, memory_id: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
        """{block_id, revision (current), claim_slot, relation|None} for one record; strict scope (VersionedMemoryNotFound)."""
        if self._local_db_path() is None:
            raise MemoryRequestRejectedError("version relation unavailable")
        def reader(repo, scope, block):
            current = repo.get_knowledge(owner=scope.owner, block_id=block, workspace_id=scope.workspace_id)
            return {"block_id": block, "revision": current["revision"], "claim_slot": current.get("claim_slot"),
                    "relation": self._stored_relation(current.get("activation_rationale"))}
        return await asyncio.to_thread(self._versioned_read, memory_id, owner, reader)

    def _versioned_read(self, memory_id, owner, reader):
        """Diff/blame reads: resolve the id with the history route's strict scope (owner + workspace, a foreign,
        unknown or wrong-workspace id all raise VersionedMemoryNotFound) BEFORE calling a V2Repository reader."""
        import sqlite3
        from src.memery_v2 import V2Repository
        from src.memory_versioned import KnowledgeScope, _resolve_block_id
        scope = self._scope(owner)
        with sqlite3.connect(self._fm_db_path, timeout=30) as conn:
            block_id = _resolve_block_id(conn, KnowledgeScope(scope.owner, scope.workspace_id, ""), memory_id)
        return reader(V2Repository(self._fm_db_path), scope, block_id)

    async def versioned_diff(self, memory_id: str, *, from_revision: int, to_revision: int, owner: Optional[str] = None) -> Dict[str, Any]:
        if self._local_db_path() is None:
            raise MemoryRequestRejectedError("version diff unavailable")
        return await asyncio.to_thread(self._versioned_read, memory_id, owner, lambda repo, scope, block: repo.knowledge_diff(
            owner=scope.owner, block_id=block, from_revision=from_revision, to_revision=to_revision, workspace_id=scope.workspace_id))

    async def versioned_blame(self, memory_id: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
        if self._local_db_path() is None:
            raise MemoryRequestRejectedError("version blame unavailable")
        return await asyncio.to_thread(self._versioned_read, memory_id, owner, lambda repo, scope, block: repo.knowledge_blame(
            owner=scope.owner, block_id=block, workspace_id=scope.workspace_id))

    async def versioned_list(
        self,
        *,
        owner: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        kinds: Optional[List[str]] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        if local_db is None:
            # v2-native read; broker mode without a pinned FM_DB_PATH has
            # no safe direct store to project from.
            return []
        from src.memory_versioned import list_knowledge

        details = await asyncio.to_thread(
            list_knowledge,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            statuses=statuses,
            kinds=kinds,
            limit=limit,
            db_path=local_db,
        )
        # Rust's passive fuzzy resolver can archive a legacy unknown before
        # the compatibility mirror is transitioned.  Never expose that split
        # as an active v2 question: hide only the stale OPEN-question row until
        # the canonical resolution/forget coordinator reconciles its lineage.
        # A resolved question (status active) archives its compatibility row
        # by design and must stay visible — its answer is the current value.
        try:
            live_ids = await asyncio.to_thread(
                _live_legacy_ids, local_db, scope.owner, scope.workspace_id
            )
            def guarded_seed_inquiry(item):
                if not self._guarded_v17 or type(item) is not dict:
                    return False
                current=item.get('current'); history=item.get('history')
                if type(current) is not dict or type(history) is not list:
                    return False
                rationale=current.get('activation_rationale')
                question=rationale.get('question_context') if type(rationale) is dict else None
                provenance=question.get('provenance') if type(question) is dict else None
                block=item.get('block_id')
                creation=[r for r in history if type(r) is dict and r.get('revision')==1]
                return (type(provenance) is dict and set(provenance)=={'version','kind','seed_key'}
                    and type(provenance['version']) is int and provenance['version']==1 and provenance['kind']=='system_seed'
                    and provenance['seed_key'] in ('handler.preferred_address','assistant.operating_boundary')
                    and type(block) is str and re.fullmatch(r'question_seed_[0-9a-f]{32}',block) is not None
                    and len(creation)==1 and creation[0].get('actor_type')=='system'
                    and creation[0].get('actor_id')=='memory-question-seed'
                    and creation[0].get('source_event_id') is None and not creation[0].get('evidence_ids')
                    and current.get('source_event_id') is None and not current.get('evidence_ids'))
            details = [
                item for item in details
                if not (
                    item.get("current", {}).get("kind") == "open_question"
                    and item.get("current", {}).get("status") == "open"
                    and str(item.get("id")) not in live_ids
                    and not guarded_seed_inquiry(item)
                )
            ]
        except Exception:
            logger.debug("legacy/v2 question reconciliation unavailable", exc_info=True)
        return details

    async def versioned_transition(
        self,
        memory_id: str,
        action: str,
        *,
        expected_revision: int,
        owner: Optional[str] = None,
        published_review: Any = None,
        **changes: Any,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        if self._local_db_path() is None:
            if self._guarded_v17 or published_review is not None:
                raise ValueError("guarded review requires local authenticated authority")
            return await self._call_tool("versioned_transition", {"memory_id": memory_id, "action": action, "expected_revision": expected_revision, **changes, "owner": scope.owner, "workspace_id": scope.workspace_id})
        from src.memory_versioned import transition_knowledge

        return await asyncio.to_thread(
            transition_knowledge,
            memory_id,
            action,
            owner=scope.owner,
            workspace_id=scope.workspace_id,
            expected_revision=expected_revision,
            db_path=self._fm_db_path,
            guarded_v17=self._guarded_v17,
            published_review=published_review,
            **changes,
        )

    async def assign_trust(
        self,
        *,
        owner: str,
        subject_kind: str,
        subject_id: str,
        subject_revision: int,
        trust: Optional[float] = None,
        state: str = "assigned",
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
        reason_code: str = "owner_review",
        rationale: str = "",
        evidence_ids: Optional[list[str]] = None,
        expected_assignment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append an owner-relative Trust assignment in the v2 authority."""
        scope = self._scope(owner)
        if self._local_db_path() is None:
            return await self._call_tool("assign_trust", {"subject_kind": subject_kind, "subject_id": subject_id, "subject_revision": subject_revision, "project_id": project_id, "trust": trust, "state": state, "reason_code": reason_code, "rationale": rationale, "evidence_ids": evidence_ids, "expected_assignment_id": expected_assignment_id, "owner": scope.owner, "workspace_id": workspace_id or scope.workspace_id})
        from src.memery_v2 import V2Repository

        return await asyncio.to_thread(
            V2Repository(self._fm_db_path).assign_trust,
            owner=owner,
            subject_kind=subject_kind,
            subject_id=subject_id,
            subject_revision=subject_revision,
            trust=trust,
            state=state,
            workspace_id=workspace_id,
            project_id=project_id,
            actor_type="owner",
            actor_id=owner,
            reason_code=reason_code,
            rationale=rationale,
            evidence_ids=evidence_ids,
            expected_assignment_id=expected_assignment_id,
        )

    async def get_trust(
        self,
        *,
        owner: str,
        subject_kind: str,
        subject_id: str,
        subject_revision: int,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read the current owner-relative Trust assignment."""
        scope = self._scope(owner)
        if self._local_db_path() is None:
            return await self._call_tool("get_trust", {"subject_kind": subject_kind, "subject_id": subject_id, "subject_revision": subject_revision, "project_id": project_id, "owner": scope.owner, "workspace_id": workspace_id or scope.workspace_id})
        from src.memery_v2 import V2Repository

        return await asyncio.to_thread(
            V2Repository(self._fm_db_path).get_trust,
            owner=owner,
            subject_kind=subject_kind,
            subject_id=subject_id,
            subject_revision=subject_revision,
            workspace_id=workspace_id,
            project_id=project_id,
        )

    async def get(self, memory_id: str, *, owner: Optional[str] = None) -> Optional[MemoryRecord]:
        scope = self._scope(owner)
        local_db = self._local_db_path()
        v2_read_mode = os.environ.get("FM_V2_READ_MODE", "preferred").strip().lower()
        if v2_read_mode in {"preferred", "strict"} and local_db is not None:
            from src.memery_v2 import get_current_record
            row = await asyncio.to_thread(
                get_current_record,
                memory_id,
                owner=scope.owner,
                workspace_id=scope.workspace_id,
                require_legacy_presence=True,
                db_path=local_db,
            )
            if row is not None:
                return await self._overlay_legacy_fields(self._record(row), owner=owner)
            if v2_read_mode == "strict":
                return None
        result = await self._call_tool(
            "get_memory",
            {"id": memory_id, "owner": scope.owner, "workspace_id": scope.workspace_id},
        )
        record = result.get("record")
        return self._record(record) if isinstance(record, dict) else None

    async def _get_legacy_current(
        self,
        memory_id: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[MemoryRecord]:
        """Read the post-mutation legacy row without the v2 read preference.

        During the compatibility window the legacy Rust provider is still the
        mutation transport.  Calling ``get`` after an update would therefore
        return the older v2 shadow row and mirror stale content back over the
        successful edit.  This private path is deliberately only used to
        snapshot the transport result before the v2 projection is refreshed.
        """
        scope = self._scope(owner)
        result = await self._call_tool(
            "get_memory",
            {
                "id": memory_id,
                "owner": scope.owner,
                "workspace_id": workspace_id or scope.workspace_id,
            },
        )
        record = result.get("record")
        return self._record(record) if isinstance(record, dict) else None

    async def delete(self, memory_id: str, *, owner: Optional[str] = None) -> bool:
        scope = self._scope(owner)
        prior = None
        try:
            prior = await self.get(memory_id, owner=owner)
        except Exception:
            logger.debug("could not snapshot memory before delete", exc_info=True)
        result = await self._call_tool("delete_memory", {
            "id": memory_id,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        })
        deleted = bool(result.get("deleted"))
        if deleted and prior is not None and self._local_db_path() is not None:
            try:
                from src.memery_v2 import mirror_record
                mirror_record(prior, owner=scope.owner, workspace_id=scope.workspace_id, action="delete", db_path=self._fm_db_path)
            except Exception:
                logger.debug("v2 delete mirror unavailable", exc_info=True)
        return deleted

    async def update(
        self,
        memory_id: str,
        *,
        text: Optional[str] = None,
        category: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> Optional[MemoryRecord]:
        scope = self._scope(owner)
        result = await self._call_tool("update_memory", {
            "id": memory_id,
            "content": text,
            "category": category,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        })
        if not result.get("updated"):
            return None
        record = await self._get_legacy_current(memory_id, owner=owner)
        if record is not None and self._local_db_path() is not None:
            try:
                from src.memery_v2 import mirror_record
                mirror_record(record, owner=scope.owner, workspace_id=scope.workspace_id, db_path=self._fm_db_path)
            except Exception:
                logger.debug("v2 update mirror unavailable", exc_info=True)
        return record

    async def pin(self, memory_id: str, pinned: bool, *, owner: Optional[str] = None) -> bool:
        scope = self._scope(owner)
        result = await self._call_tool("update_memory", {
            "id": memory_id,
            "pinned": bool(pinned),
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        })
        updated = bool(result.get("updated"))
        if updated and self._local_db_path() is not None:
            try:
                record = await self._get_legacy_current(memory_id, owner=owner)
                if record is not None:
                    metadata = dict(record.metadata or {})
                    metadata["pinned"] = bool(pinned)
                    record.metadata = metadata
                    record.pinned = bool(pinned)
                    from src.memery_v2 import mirror_record
                    mirror_record(record, owner=scope.owner, workspace_id=scope.workspace_id, db_path=self._fm_db_path)
            except Exception:
                logger.debug("v2 pin mirror unavailable", exc_info=True)
        return updated

    async def _host_published_preview(self, *, owner, block_id, action, expected_revision, target_revision, request_key, approved_paths=None, account_id=None):
        from src.memory_versioned import prepare_published_lifecycle
        scope=self._scope(owner)
        if not self._guarded_v17 or self._broker_url or not self._local_db_path():
            raise MemoryRequestRejectedError('published review unavailable')
        return await asyncio.to_thread(prepare_published_lifecycle,db_path=self._fm_db_path,owner=scope.owner,
            workspace_id=scope.workspace_id,block_id=block_id,action=action,expected_revision=expected_revision,
            target_revision=target_revision,request_key=request_key,approved_paths=approved_paths,account_id=account_id)

    async def _host_published_apply(self, review, *, block_id, action, expected_revision, target_revision, operation_json, operation_sha256):
        from src.memory_versioned import PublishedReviewRequest
        if type(review) is not PublishedReviewRequest or not callable(getattr(review,'before_issue',None)) or self._scope(review.owner).owner!=review.owner or review.workspace_id!=self._workspace_id:
            raise MemoryRequestRejectedError('published review authority unavailable')
        preview=await self._host_published_preview(owner=review.owner,block_id=block_id,action=action,
            expected_revision=expected_revision,target_revision=target_revision,request_key=review.request_key,approved_paths=review.approved_paths,
            account_id=review.account_id if action in ('question_defer','question_dismiss','question_reopen') else None)
        if (type(operation_json) is not str or type(operation_sha256) is not str or preview['operation_json']!=operation_json
            or preview['operation_sha256']!=operation_sha256):
            raise MemoryRequestRejectedError('published review payload changed')
        body={'version':1,'owner':review.owner,'workspace_id':review.workspace_id,'project_id':None,'block_id':block_id,
              'action':action,'expected_revision':expected_revision,'target_revision':target_revision,'request_key':review.request_key,
              'reason':preview['reason'],'approved_paths':list(review.approved_paths),'operation_json':operation_json}
        if action in ('question_defer','question_dismiss','question_reopen'):
            account=review.account_id
            if type(account) is not str or not account or account!=account.strip() or len(account.encode('utf-8'))>512:
                raise MemoryRequestRejectedError('question review account unavailable')
            from src.memory_versioned import question_review_operation_key
            body['account_id']=account
            body['original_request_key']=review.request_key
            body['request_key']=question_review_operation_key(account,review.request_key)
        result=await self._host_candidate_request(review.owner,review.workspace_id,{'phase':'published_lifecycle_apply','request':body},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=review.before_issue)
        if (type(result) is not dict or set(result)!={'version','state','block_id','applied_revision','current_revision','visibility'}
            or type(result['version']) is not int or result['version']!=1 or result['state'] not in ('applied','already_applied')
            or result['block_id']!=block_id or type(result['applied_revision']) is not int or result['applied_revision']!=expected_revision+1
            or type(result['current_revision']) is not int or result['current_revision']<result['applied_revision'] or result['visibility']!='not_rechecked'):
            raise MemoryRequestRejectedError('published review receipt unavailable')
        return result

    async def _host_question_seed_bootstrap(self, *, owner, account_id, request_key, coordinator, current, incognito, no_memory, write_deadline):
        """Fixed host-only issuer; current must be the live context authority."""
        from src.openclank.memory_policy_projection import PolicyTransitionCoordinator, policy_coordinator
        from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
        from src.openclank.memory_policy import STORE
        scope=self._scope(owner)
        if (not self._guarded_v17 or self._broker_url or type(coordinator) is not PolicyTransitionCoordinator
            or coordinator is not policy_coordinator() or not callable(current)
            or type(incognito) is not bool or type(no_memory) is not bool or incognito or no_memory
            or type(account_id) is not str or not account_id or type(request_key) is not str or not request_key
            or request_key!=request_key.strip() or len(request_key)>512 or type(write_deadline) is not float
            or not math.isfinite(write_deadline) or asyncio.get_running_loop().time()>=write_deadline
            or not self._host_bounded_write_available() or not current()):
            raise MemoryRequestRejectedError('question seed authority unavailable')
        with PREFERENCE_LOCK:
            prefs=_load_for_user(owner)
            enabled=prefs.get('memory_question_seeds',True)
            if type(enabled) is not bool or not enabled:
                raise MemoryRequestRejectedError('question seeds disabled or unavailable')
        policy=STORE.effective_snapshot(owner,read_privilege=True,write_privilege=True,
            incognito=incognito,no_memory=no_memory,compare_mode=False)
        if not policy.get('read_allowed') or not policy.get('direct_write_allowed') or not current():
            raise MemoryRequestRejectedError('question seed policy unavailable')
        refs=coordinator.checked_refs(owner,account_id,scope.workspace_id)
        # Bind the effective preference to those exact references: an Off
        # transition completed between the first read and checked_refs must
        # not turn its new fingerprint into authority for the old On value.
        with PREFERENCE_LOCK:
            fresh=_load_for_user(owner).get('memory_question_seeds',True)
            if type(fresh) is not bool or not fresh:
                raise MemoryRequestRejectedError('question seeds disabled or unavailable')
        fresh_policy=STORE.effective_snapshot(owner,read_privilege=True,write_privilege=True,
            incognito=incognito,no_memory=no_memory,compare_mode=False)
        if (not fresh_policy.get('read_allowed') or not fresh_policy.get('direct_write_allowed')
            or coordinator.checked_refs(owner,account_id,scope.workspace_id)!=refs or not current()):
            raise MemoryRequestRejectedError('question seed authority changed')
        # This is positive-only and read-only. Unknown/ambiguous state does not
        # establish absence; native remains the authoritative creation path.
        from services.memory.principal_context import guarded_seed_slots_present
        try:
            present=await asyncio.wait_for(asyncio.to_thread(guarded_seed_slots_present,
                preparation=self._guarded_preparation,owner=scope.owner,account_id=account_id,workspace_id=scope.workspace_id),timeout=0.1)
        except TimeoutError as exc:
            raise MemoryRequestRejectedError('question seed preflight unavailable') from exc
        if present:
            if not current() or coordinator.checked_refs(owner,account_id,scope.workspace_id)!=refs:
                raise MemoryRequestRejectedError('question seed authority changed')
            return {'version':1,'state':'slots_present'}
        body={'version':2,'owner':scope.owner,'account_id':account_id,'workspace_id':scope.workspace_id,'request_key':request_key,'policy_projection':refs}
        result=await self._host_candidate_request(scope.owner,scope.workspace_id,
            {'phase':'question_seed_bootstrap','request':body},require_live=True,write_deadline=write_deadline)
        if (not current() or coordinator.checked_refs(owner,account_id,scope.workspace_id)!=refs
            or type(result) is not dict or set(result)!={'version','state','assistant_entity_id','handler_entity_id','created','visibility'}
            or type(result['version']) is not int or result['version']!=1 or result['state'] not in ('applied','already_applied') or result['visibility']!='not_rechecked'
            or any(type(result[k]) is not str or not result[k] for k in ('assistant_entity_id','handler_entity_id'))
            or type(result['created']) is not int or result['created'] not in (0,1,2)):
            raise MemoryRequestRejectedError('question seed receipt unavailable')
        return result

    async def _host_question_seed_status(self, *, owner, account_id, original_request, coordinator, current):
        """Historical proof only; not_found never authorizes redispatch."""
        from src.openclank.memory_policy_projection import PolicyTransitionCoordinator, policy_coordinator
        scope=self._scope(owner)
        if (type(coordinator) is not PolicyTransitionCoordinator or coordinator is not policy_coordinator()
            or not callable(current) or not current() or type(original_request) is not dict
            or set(original_request)!={'version','owner','account_id','workspace_id','request_key','policy_projection'}
            or original_request.get('owner')!=scope.owner or original_request.get('account_id')!=account_id
            or original_request.get('workspace_id')!=scope.workspace_id):
            raise MemoryRequestRejectedError('question seed status authority unavailable')
        refs=coordinator.checked_refs(owner,account_id,scope.workspace_id)
        result=await self._host_candidate_request(scope.owner,scope.workspace_id,{'phase':'question_seed_status',
            'request':{'request':original_request,'current_policy':refs}},require_live=True)
        if not current() or coordinator.checked_refs(owner,account_id,scope.workspace_id)!=refs:
            raise MemoryRequestRejectedError('question seed status authority changed')
        if type(result) is dict and set(result)=={'version','state'} and type(result['version']) is int and result=={'version':1,'state':'not_found'}:
            return result
        if (type(result) is not dict or set(result)!={'version','state','assistant_entity_id','handler_entity_id','created','visibility'}
            or type(result['version']) is not int or result['version']!=1 or result['state']!='already_applied'
            or result['visibility']!='not_rechecked' or type(result['created']) is not int or result['created'] not in (0,1,2)
            or any(type(result[k]) is not str or not result[k] for k in ('assistant_entity_id','handler_entity_id'))):
            raise MemoryRequestRejectedError('question seed status unavailable')
        return result

    async def _host_confirmed_question(self, request):
        """Apply only an exact named-user review through the guarded native writer."""
        from src.memory_versioned import ConfirmedQuestionReview, PublishedReviewRequest
        from src.memery_v2 import guarded_update_operation, _sealed_json
        if type(request) is not ConfirmedQuestionReview or type(request.review) is not PublishedReviewRequest or not callable(getattr(request.review,'before_issue',None)) or not self._guarded_v17 or self._broker_url:
            raise MemoryRequestRejectedError('confirmed question review unavailable')
        review=request.review; scope=self._scope(review.owner)
        account_id=getattr(review,'account_id',None)
        if account_id is not None and (type(account_id) is not str or not account_id or account_id!=account_id.strip() or len(account_id)>512):
            raise MemoryRequestRejectedError('confirmed question account unavailable')
        if (scope.owner!=review.owner or scope.workspace_id!=review.workspace_id or type(request.expected_revision) is not int or request.expected_revision<1
            or any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in
                (request.block_id,request.subject_entity_id,request.predicate,request.claim_slot,review.request_key))
            or type(request.answer) is not str or not request.answer.strip() or len(request.answer.encode('utf-8'))>8192
            or type(review.approved_paths) is not tuple or not review.approved_paths or len(review.approved_paths)>32
            or any(type(v) is not str or not v.startswith('/') for v in review.approved_paths)):
            raise MemoryRequestRejectedError('confirmed question review unavailable')
        try:
            capability=await self._host_candidate_request(review.owner,review.workspace_id,
                {'phase':'question_semantics_status'},require_live=True)
        except Exception as exc:
            raise MemoryRequestRejectedError('question semantics unavailable') from exc
        if (type(capability) is not dict or set(capability)!={'version','qualified'}
            or type(capability['version']) is not int or capability['version']!=1 or capability['qualified'] is not True):
            raise MemoryRequestRejectedError('question semantics unavailable')
        detail=await self.versioned_detail(request.block_id,owner=review.owner)
        history=detail.get('history') if type(detail) is dict else None
        matches=[item for item in history if type(item) is dict and item.get('revision')==request.expected_revision] if type(history) is list else []
        current=matches[0] if len(matches)==1 else None
        if (type(current) is not dict or detail.get('block_id')!=request.block_id or detail.get('owner')!=review.owner
            or detail.get('workspace_id')!=review.workspace_id or detail.get('project_id') is not None
            or current.get('kind')!='open_question' or current.get('status')!='open'
            or current.get('subject_entity_id')!=request.subject_entity_id or current.get('predicate')!=request.predicate
            or current.get('claim_slot')!=request.claim_slot):
            raise MemoryRequestRejectedError('question identity or revision changed')
        if current.get('expected_value_type')!='string':
            raise MemoryRequestRejectedError('question source unavailable')
        source=current.get('source_event_id')
        if source is None:
            if current.get('evidence_ids'):
                raise MemoryRequestRejectedError('question source unavailable')
        else:
            if type(source) is not str:
                raise MemoryRequestRejectedError('question source unavailable')
            if source.startswith('memory:'):
                curated=source[len('memory:'):];expected_version='s03.explicit-question-mirror.v1'
            elif source.startswith('memory-import-review:'):
                evidence=current.get('evidence_ids')
                match=re.fullmatch(r'evidence_(.+)_1',evidence[0]) if type(evidence) is list and len(evidence)==1 and type(evidence[0]) is str else None
                if match is None: raise MemoryRequestRejectedError('question source unavailable')
                curated=match.group(1);expected_version='s03.import-question-mirror.v1'
            else: raise MemoryRequestRejectedError('question source unavailable')
            if not curated or curated!=curated.strip() or len(curated)>512:
                raise MemoryRequestRejectedError('question source unavailable')
            # The evidence ID selects a possible record; Native independently
            # verifies original receipt, evidence and the current exact mirror.
            selector={'phase':'mirror_context','curated_id':curated}
            if account_id is not None:
                selector['account_id']=account_id
                selector['question_answer_block_id']=request.block_id
            context=await self._host_candidate_request(review.owner,review.workspace_id,selector,require_live=True)
            binding=context.get('binding') if type(context) is dict else None
            if (type(context) is not dict or context.get('mode')!='guarded' or type(binding) is not dict
                or binding.get('version') not in (expected_version, 's09.question-review-successor.v1')
                or (account_id is None and binding.get('version')!=expected_version)
                or (account_id is not None and context.get('reviewer_account_id')!=account_id)
                or binding.get('owner')!=review.owner or binding.get('workspace')!=review.workspace_id
                or binding.get('project')!='' or binding.get('curated_id')!=curated
                or binding.get('block_id')!=request.block_id or type(binding.get('revision')) is not int
                or binding['revision']!=request.expected_revision or context.get('expected_revision')!=request.expected_revision
                or type(binding.get('field_map')) is not dict
                or binding['field_map'].get('content')!='/activation_rationale/question_text'
                or any(type(context.get(k)) is not str or not context[k] for k in ('candidate_id','origin_request_key','prior_receipt_id','reviewer_account_id'))):
                raise MemoryRequestRejectedError('question source unavailable')
        payload={'value':{'type':'string','value':request.answer},'status':'active'}
        operation=guarded_update_operation(owner=review.owner,workspace=review.workspace_id,project='',block_id=request.block_id,
            expected_revision=request.expected_revision,payload=payload,request_key=review.request_key,actor_type='user',actor_id=review.owner,
            reason='answer open question',source_event_id=None,approved_paths=review.approved_paths)
        body={'version':1,'owner':review.owner,'workspace_id':review.workspace_id,'project_id':None,'block_id':request.block_id,
            'subject_entity_id':request.subject_entity_id,'predicate':request.predicate,'claim_slot':request.claim_slot,
            'expected_revision':request.expected_revision,'answer':request.answer,'request_key':review.request_key,
            'approved_paths':list(review.approved_paths),'operation_json':_sealed_json(operation)}
        if account_id is not None: body['account_id']=account_id
        result=await self._host_candidate_request(review.owner,review.workspace_id,{'phase':'confirmed_question_apply','request':body},require_live=True,write_deadline=asyncio.get_running_loop().time()+1.5,before_issue=review.before_issue)
        question=result.get('question') if type(result) is dict else None
        if (type(result) is not dict or set(result)!={'version','question','decision'} or result['version']!=1 or type(question) is not dict
            or question.get('identity',{}).get('block_id')!=request.block_id or question.get('revision')!=request.expected_revision+1
            or question.get('payload',{}).get('status')!='active' or question.get('payload',{}).get('value')!={'type':'string','value':request.answer}):
            raise MemoryRequestRejectedError('confirmed question receipt unavailable')
        return result

    async def resolve_question(
        self,
        memory_id: str,
        *,
        resolved_by: Optional[str] = None,
        answer: Optional[str] = None,
        expected_revision: Optional[int] = None,
        owner: Optional[str] = None,
    ) -> bool:
        scope = self._scope(owner)
        if self._local_db_path() is None:
            result = await self._call_tool("resolve_question", {
                "memory_id": memory_id, "resolved_by": resolved_by, "answer": answer,
                "expected_revision": expected_revision, "owner": scope.owner,
                "workspace_id": scope.workspace_id,
            })
            return bool(result.get("resolved"))
        prior = await self._get_legacy_current(memory_id, owner=owner)
        if prior is None:
            return False
        if prior.archived or prior.kind not in {"unknown", "open_question"}:
            return False
        answer_text = str(answer or "").strip()
        if not answer_text and resolved_by:
            answer_record = await self._get_legacy_current(resolved_by, owner=owner)
            if answer_record is not None:
                answer_text = answer_record.text.strip()
        if not answer_text:
            raise MemoryRequestRejectedError("resolving an open question requires its answer")
        try:
            detail = await self.versioned_detail(memory_id, owner=owner)
        except Exception:
            from src.memery_v2 import mirror_record

            mirrored = await asyncio.to_thread(
                mirror_record,
                prior,
                owner=scope.owner,
                workspace_id=scope.workspace_id,
                db_path=self._fm_db_path,
            )
            if not mirrored:
                raise MemoryRequestRejectedError("open question has no canonical knowledge block")
            detail = await self.versioned_detail(memory_id, owner=owner)
        current_revision = int(detail["current_revision"])
        if expected_revision is not None and int(expected_revision) != current_revision:
            from src.memory_versioned import VersionedMemoryConflict

            raise VersionedMemoryConflict(int(expected_revision), detail["current"])
        resolved_detail = await self.versioned_transition(
            memory_id,
            "resolve",
            expected_revision=current_revision,
            value=answer_text,
            actor_type="ai" if resolved_by else "user",
            actor_id=resolved_by or scope.owner,
            reason="answer open question",
            owner=owner,
        )
        args: Dict[str, Any] = {
            "id": memory_id,
            # Memery workers validate that an archive transition carries
            # the answer; older builds ignore this additive field.
            "answer": answer_text,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        }
        if resolved_by:
            args["resolved_by"] = resolved_by
        try:
            result = await self._call_tool("resolve_memory", args)
        except Exception:
            try:
                await self.versioned_transition(
                    memory_id,
                    "reopen",
                    expected_revision=int(resolved_detail["current_revision"]),
                    actor_type="system",
                    actor_id="resolve-compensation",
                    reason="legacy resolve failed",
                    owner=owner,
                )
            except Exception:
                logger.exception("failed to compensate canonical question resolution")
            raise
        if not bool(result.get("resolved")):
            await self.versioned_transition(
                memory_id,
                "reopen",
                expected_revision=int(resolved_detail["current_revision"]),
                actor_type="system",
                actor_id="resolve-compensation",
                reason="legacy resolve rejected",
                owner=owner,
            )
            return False
        return True

    async def reopen_question(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        owner: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Reopen the same canonical block and its compatibility row."""
        scope = self._scope(owner)
        reopened_detail = await self.versioned_transition(
            memory_id,
            "reopen",
            expected_revision=expected_revision,
            actor_type="user",
            actor_id=scope.owner,
            reason="reopen question",
            owner=owner,
        )
        try:
            result = await self._call_tool(
                "reopen_memory",
                {
                    "id": memory_id,
                    "owner": scope.owner,
                    "workspace_id": scope.workspace_id,
                },
            )
        except Exception:
            try:
                await self.versioned_transition(
                    memory_id,
                    "revert",
                    expected_revision=int(reopened_detail["current_revision"]),
                    target_revision=int(expected_revision),
                    actor_type="system",
                    actor_id="reopen-compensation",
                    reason="legacy reopen failed",
                    owner=owner,
                )
            except Exception:
                logger.exception("failed to compensate canonical question reopen")
            raise
        if not bool(result.get("reopened")):
            await self.versioned_transition(
                memory_id,
                "revert",
                expected_revision=int(reopened_detail["current_revision"]),
                target_revision=int(expected_revision),
                actor_type="system",
                actor_id="reopen-compensation",
                reason="legacy reopen rejected",
                owner=owner,
            )
            raise MemoryRequestRejectedError("resolved question could not be reopened")
        return reopened_detail

    async def record_access(
        self,
        memory_ids: List[str],
        *,
        owner: Optional[str] = None,
    ) -> int:
        scope = self._scope(owner)
        result = await self._call_tool(
            "record_memory_access",
            {"ids": memory_ids, "owner": scope.owner, "workspace_id": scope.workspace_id},
        )
        return int(result.get("updated", 0))

    async def _host_owner_transition(self, request: _HostOwnerTransition, *, capability, status: bool = False) -> Dict[str, Any]:
        """Guarded owner-wide operation/status; status never authorizes a retry."""
        import re
        from src.openclank.chat_source_bridge import HostAccountSourceTransition
        bounded = lambda value: type(value) is str and value and value == value.strip() and len(value) <= 512
        digest = lambda value: type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None
        receipt_id = lambda value: type(value) is str and re.fullmatch(r'owner_transition_[0-9a-f]{64}', value) is not None
        relocation = request.rag_relocation if type(request) is _HostOwnerTransition else None
        valid_relocation = relocation is None or (type(relocation) is dict and set(relocation) == {'from', 'to'}
            and all(type(value) is str and value != '/' and os.path.isabs(value)
                and os.path.normpath(value) == value and '\x00' not in value for value in relocation.values())
            and relocation['from'] != relocation['to']
            and not relocation['from'].startswith(relocation['to'] + os.sep)
            and not relocation['to'].startswith(relocation['from'] + os.sep))
        if (type(request) is not _HostOwnerTransition or type(capability) is not HostAccountSourceTransition
                or not valid_relocation
                or type(status) is not bool or type(request.version) is not int or request.version != 1
                or type(request.generation) is not int or request.generation < 1 or request.action not in ('forward', 'reverse')
                or not all(bounded(value) for value in (request.database_id, request.account_id, request.operation_id,
                    request.step, request.owner, request.new_owner))
                or request.owner == request.new_owner or not digest(request.expected_source_fingerprint)
                or not digest(request.expected_target_fingerprint)
                or ((request.prior_receipt_id is None) != (request.prior_receipt_hash is None))
                or ((request.action == 'reverse' or request.generation > 1) and not receipt_id(request.prior_receipt_id))
                or (request.prior_receipt_id is not None and (not receipt_id(request.prior_receipt_id) or not digest(request.prior_receipt_hash)))
                or (request.action == 'forward' and request.generation == 1 and request.prior_receipt_id is not None)
                or capability.operation_id != request.operation_id or self._scope(request.owner).owner != request.owner
                or self._scope(request.new_owner).owner != request.new_owner):
            raise MemoryRequestRejectedError('host owner transition unavailable')
        from src.memery_database import GuardedStorePreparation
        from src.memery_external_sources import _queued_owned_thread
        preparation=self._guarded_preparation; transport=self._owned_transport; generation=self._owned_generation; task=self._owner_task
        if (type(preparation) is not GuardedStorePreparation or preparation.database_id!=request.database_id
            or transport is None or transport.process is None or task is None or task.done()
            or os.path.realpath(self._local_db_path() or '')!=preparation.database_path):
            raise MemoryRequestRejectedError('host owner transition unavailable')
        process=transport.process
        def current():
            if (self._guarded_preparation is not preparation or self._owned_transport is not transport
                or self._owned_generation is not generation or self._owner_task is not task or task.done()
                or transport.process is not process or process.returncode is not None or not transport.current(generation)):
                raise MemoryRequestRejectedError('host owner transition changed')
            operation=capability.lifecycle.get_operation(request.operation_id)
            if operation.get('subject_id')!=request.account_id:
                raise MemoryRequestRejectedError('host owner transition account changed')
            capability.assert_current(provider=self,action='rename',owner=request.owner,new_owner=request.new_owner)
        current()
        if status:
            await _queued_owned_thread(preparation._owned_move_schema_cookie)
        else:
            await _queued_owned_thread(preparation.assert_valid)
        current()
        response = await self._host_candidate_request(request.owner, self._workspace_id,
            {'phase':'owner_transition_status' if status else 'owner_transition_apply', 'request':asdict(request)}, require_live=True)
        if status and response == {'version':1, 'state':'not_found'}:
            return response
        if (type(response) is not dict or set(response) != {'version','state','receipt_id','receipt_hash','result'}
                or type(response['version']) is not int or response['version'] != 1 or response['state'] != 'applied'
                or not receipt_id(response['receipt_id']) or not digest(response['receipt_hash']) or type(response['result']) is not dict):
            raise MemoryRequestRejectedError('host owner transition receipt unavailable')
        result = response['result']
        if (set(result) != {'action','source_before_fingerprint','target_before_fingerprint','source_after_fingerprint','target_after_fingerprint'}
                or result['action'] != request.action or result['source_before_fingerprint'] != request.expected_source_fingerprint
                or result['target_before_fingerprint'] != request.expected_target_fingerprint
                or not digest(result['source_after_fingerprint']) or not digest(result['target_after_fingerprint'])):
            raise MemoryRequestRejectedError('host owner transition receipt changed')
        current()
        fact=await self._host_candidate_request(request.owner,self._workspace_id,
            {'phase':'owner_transition_stamp','request':asdict(request)},require_live=True)
        current()
        try:
            await _queued_owned_thread(preparation._accept_owned_move_stamp,fact,request=request,receipt=response,
                                       capability=capability,provider=self)
        except (OSError,ValueError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('host owner transition stamp unavailable') from exc
        current()
        return response

    async def _host_original_owner_cache_cleanup(self, request, *, capability, receipt):
        """Reconcile only the checkpointed, still-current original operation."""
        from src.openclank.chat_source_bridge import HostAccountSourceTransition
        from src.memery_external_sources import _queued_owned_thread
        if (type(capability) is not HostAccountSourceTransition or type(receipt) is not dict
            or receipt.get('state')!='applied' or type(receipt.get('receipt_id')) is not str
            or type(receipt.get('receipt_hash')) is not str):
            raise MemoryRequestRejectedError('owner cache cleanup unavailable')
        if type(request) is _HostOwnerTransition:
            action='rename'; phase='owner_transition_cache_cleanup'
            step=request.step if request.action=='forward' else 'memory_fm_reverse_'+str(request.generation)
            destination=request.new_owner
        elif type(request) is _HostOwnerPurge:
            action='purge'; phase='owner_purge_cache_cleanup'; step=request.step;destination=None
        else: raise MemoryRequestRejectedError('owner cache cleanup unavailable')
        if capability.operation_id!=request.operation_id:
            raise MemoryRequestRejectedError('owner cache cleanup unavailable')
        if action=='rename':
            expected_phase={key:getattr(request,key) for key in ('database_id','account_id','operation_id','step','generation','action','owner','new_owner')}
            if capability.phase!=expected_phase:
                raise MemoryRequestRejectedError('owner cache cleanup unavailable')
        preparation=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        if (preparation is None or preparation.database_id!=request.database_id or transport is None
            or transport.process is None or task is None or task.done()):
            raise MemoryRequestRejectedError('owner cache cleanup unavailable')
        process=transport.process
        def current():
            try:
                if (self._guarded_preparation is not preparation or self._owned_transport is not transport
                    or self._owned_generation is not generation or self._owner_task is not task or task.done()
                    or transport.process is not process or process.returncode is not None or not transport.current(generation)):
                    return False
                capability.assert_current(provider=self,action=action,owner=request.owner,new_owner=destination)
                operation=capability.lifecycle.get_operation(request.operation_id)
                saved=(operation.get('steps') or {}).get(step) or {}
                if (operation.get('subject_id')!=request.account_id or saved.get('state')!='applied'
                    or saved.get('receipt')!=receipt): return False
                preparation.assert_valid()
                return True
            except Exception:return False
        if not await _queued_owned_thread(current): raise MemoryRequestRejectedError('owner cache cleanup authority changed')
        result=await self._host_candidate_request(request.owner,self._workspace_id,{'phase':phase,'request':asdict(request)},
            require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),before_issue=current)
        if (not await _queued_owned_thread(current) or result!={'version':1,'state':'cleaned',
            'receipt_id':receipt['receipt_id'],'receipt_hash':receipt['receipt_hash']}):
            raise MemoryRequestRejectedError('owner cache cleanup unavailable')
        return result

    async def _host_owner_transition_cache_cleanup(self, request, *, capability, receipt):
        if type(request) is not _HostOwnerTransition: raise MemoryRequestRejectedError('owner cache cleanup unavailable')
        return await self._host_original_owner_cache_cleanup(request,capability=capability,receipt=receipt)

    async def _host_owner_purge_cache_cleanup(self, request, *, capability, receipt):
        if type(request) is not _HostOwnerPurge: raise MemoryRequestRejectedError('owner cache cleanup unavailable')
        return await self._host_original_owner_cache_cleanup(request,capability=capability,receipt=receipt)

    async def _host_owner_purge(self, request: _HostOwnerPurge, *, capability, status: bool = False) -> Dict[str, Any]:
        """Guarded owner-wide purge/status under the exact account claim."""
        import re
        from src.openclank.chat_source_bridge import HostAccountSourceTransition
        bounded = lambda value: type(value) is str and value and value == value.strip() and len(value) <= 512
        digest = lambda value: type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None
        if (type(request) is not _HostOwnerPurge or type(capability) is not HostAccountSourceTransition or type(status) is not bool
                or type(request.version) is not int or request.version != 1 or type(request.generation) is not int
                or request.generation < 1 or request.action != 'purge'
                or not all(bounded(value) for value in (request.database_id, request.account_id, request.operation_id, request.step, request.owner))
                or not digest(request.expected_source_fingerprint) or capability.operation_id != request.operation_id
                or self._scope(request.owner).owner != request.owner):
            raise MemoryRequestRejectedError('host owner purge unavailable')
        from src.memery_database import GuardedStorePreparation
        from src.memery_external_sources import _queued_owned_thread
        preparation=self._guarded_preparation; transport=self._owned_transport; generation=self._owned_generation; task=self._owner_task
        if (type(preparation) is not GuardedStorePreparation or preparation.database_id!=request.database_id
            or transport is None or transport.process is None or task is None or task.done()
            or os.path.realpath(self._local_db_path() or '')!=preparation.database_path):
            raise MemoryRequestRejectedError('host owner purge unavailable')
        process=transport.process
        def current():
            if (self._guarded_preparation is not preparation or self._owned_transport is not transport
                or self._owned_generation is not generation or self._owner_task is not task or task.done()
                or transport.process is not process or process.returncode is not None or not transport.current(generation)):
                raise MemoryRequestRejectedError('host owner purge changed')
            operation=capability.lifecycle.get_operation(request.operation_id)
            if operation.get('subject_id')!=request.account_id:
                raise MemoryRequestRejectedError('host owner purge account changed')
            capability.assert_current(provider=self,action='purge',owner=request.owner)
        current()
        if status:
            await _queued_owned_thread(preparation._owned_move_schema_cookie)
        else:
            await _queued_owned_thread(preparation.assert_valid)
        current()
        response = await self._host_candidate_request(request.owner, self._workspace_id,
            {'phase':'owner_purge_status' if status else 'owner_purge_apply', 'request':asdict(request)}, require_live=True)
        if status and response == {'version':1, 'state':'not_found'}:
            return response
        if (type(response) is not dict or set(response) != {'version','state','receipt_id','receipt_hash','result'}
                or type(response['version']) is not int or response['version'] != 1 or response['state'] != 'applied'
                or type(response['receipt_id']) is not str or re.fullmatch(r'owner_transition_[0-9a-f]{64}', response['receipt_id']) is None
                or not digest(response['receipt_hash']) or type(response['result']) is not dict):
            raise MemoryRequestRejectedError('host owner purge receipt unavailable')
        result = response['result']
        if (set(result) != {'action','source_before_fingerprint','source_after_fingerprint'} or result['action'] != 'purge'
                or result['source_before_fingerprint'] != request.expected_source_fingerprint or not digest(result['source_after_fingerprint'])):
            raise MemoryRequestRejectedError('host owner purge receipt changed')
        current()
        fact=await self._host_candidate_request(request.owner,self._workspace_id,
            {'phase':'owner_purge_stamp','request':asdict(request)},require_live=True)
        current()
        try:
            await _queued_owned_thread(preparation._accept_owned_move_stamp,fact,request=request,receipt=response,
                                       capability=capability,provider=self)
        except (OSError,ValueError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('host owner purge stamp unavailable') from exc
        current()
        return response

    async def _host_owner_purge_stats(self, *, owner: str, capability) -> Dict[str, Any]:
        """Account-bound private inventory under the existing delete claim."""
        from src.openclank.chat_source_bridge import HostAccountSourceTransition
        from src.memery_database import GuardedStorePreparation
        from src.memery_external_sources import _queued_owned_thread
        if type(capability) is not HostAccountSourceTransition or self._scope(owner).owner!=owner:
            raise MemoryRequestRejectedError('host owner purge inventory unavailable')
        preparation=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        if type(preparation) is not GuardedStorePreparation or transport is None or transport.process is None or task is None or task.done():
            raise MemoryRequestRejectedError('host owner purge inventory unavailable')
        process=transport.process
        operation=capability.lifecycle.get_operation(capability.operation_id);account=operation.get('subject_id')
        if type(account) is not str or not account or len(account)>512:
            raise MemoryRequestRejectedError('host owner purge inventory unavailable')
        def current():
            if (self._guarded_preparation is not preparation or self._owned_transport is not transport
                    or self._owned_generation is not generation or self._owner_task is not task or task.done()
                    or transport.process is not process or process.returncode is not None or not transport.current(generation)
                    or capability.lifecycle.get_operation(capability.operation_id).get('subject_id')!=account):
                raise MemoryRequestRejectedError('host owner purge inventory changed')
            capability.assert_current(provider=self,action='purge',owner=owner)
            return True
        current();await _queued_owned_thread(preparation.assert_valid);current()
        ticket={}
        result=await self._s07_owner_rpc(owner,self._workspace_id,{'phase':'owner_purge_stats','request':{'version':1,'owner':owner,'account_id':account}},
            float(asyncio.get_running_loop().time()+30.0),read_ticket=ticket,_purge_capability=capability)
        current();await _queued_owned_thread(preparation.assert_valid);current()
        if (ticket.get('transport') is not transport or ticket.get('generation') is not generation
                or ticket.get('issued') is not True or ticket.get('frame_seen') is not True
                or ticket.get('frame_invalid') is not False or ticket.get('terminal_error') is True or type(result) is not dict
                or type(result.get('count')) is not int or result['count']<0 or type(result.get('tables')) is not dict
                or type(result.get('fingerprint')) is not str or re.fullmatch(r'[0-9a-f]{64}',result['fingerprint']) is None):
            raise MemoryRequestRejectedError('host owner purge inventory unavailable')
        return result

    async def owner_stats(self, *, owner: Optional[str] = None) -> Dict[str, Any]:
        scope = self._scope(owner)
        return await self._call_tool(
            "owner_lifecycle",
            {"action": "stats", "owner": scope.owner, "workspace_id": scope.workspace_id},
        )

    async def purge_owner(self, *, owner: Optional[str] = None, _source_capability=None) -> Dict[str, Any]:
        scope = self._scope(owner)
        if self._guarded_v17:
            from src.openclank.chat_source_bridge import HostAccountSourceTransition
            if type(_source_capability) is not HostAccountSourceTransition:
                raise MemoryRequestRejectedError('host account source transition unavailable')
            _source_capability.assert_current(provider=self,action='purge',owner=scope.owner)
        result = await self._call_tool(
            "owner_lifecycle",
            {"action": "purge", "owner": scope.owner, "workspace_id": scope.workspace_id},
        )
        if isinstance(result, dict) and result.get("purged") is True:
            await self._delete_principal_bindings(scope.owner)
        return result

    async def reset_owner(
        self,
        action: str,
        *,
        owner: Optional[str] = None,
        components: list[str],
        expected_counts: Optional[dict] = None,
        _reset_capability=None,
        _queued_retirement=None,
        _reset_closure=None,
        _reset_frozen=None,
        _reset_barrier=None,
        _reset_witness=None,
        _reset_unsent_fact=None,
    ) -> dict:
        """Own the original prequeue reservation across the whole guarded call."""
        reservation=None
        if action=='reset_commit' and self._guarded_v17:
            if _reset_unsent_fact is not None:
                self._assert_nonselective_reset_unsent(_reset_unsent_fact,_reset_capability,_reset_frozen)
                reservation=_reset_unsent_fact
            else:
                if self._reset_handoff_state.get('original') is not None:
                    raise MemoryRequestRejectedError('original reset custody unavailable')
                transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
                frozen_copy=json.loads(json.dumps(_reset_frozen,ensure_ascii=False,allow_nan=False))
                if type(frozen_copy) is not dict or type(frozen_copy.get('request')) is not dict:
                    raise MemoryRequestRejectedError('original reset custody unavailable')
                reservation=object();future=asyncio.get_running_loop().create_future();future.set_result(None)
                self._reset_handoff_state['original']={'reset_claim':_reset_capability,'reset_frozen':frozen_copy,
                    'transport':transport,'generation':generation,'reset_process':getattr(transport,'process',None),'reset_owner_task':task,
                    'reset_future':future,'issued':False,'may_issue':True,'reset_never_reserved':True,
                    'reset_reservation':reservation,'reset_unsent_drained':False,'reset_unsent_fact':None,'reset_reissued':False}
        try:
            return await self._reset_owner_impl(action,owner=owner,components=components,expected_counts=expected_counts,
                _reset_capability=_reset_capability,_queued_retirement=_queued_retirement,_reset_closure=_reset_closure,
                _reset_frozen=_reset_frozen,_reset_barrier=_reset_barrier,_reset_witness=_reset_witness,_reset_reservation=reservation)
        except BaseException:
            ticket=self._reset_handoff_state.get('original')
            if type(ticket) is dict and ticket.get('reset_reservation') is reservation and ticket.get('reset_never_reserved') is True:
                ticket['may_issue']=False;ticket['reset_unsent_drained']=True
            raise

    async def _reset_owner_impl(self, action, *, owner, components, expected_counts, _reset_capability,
            _queued_retirement, _reset_closure, _reset_frozen, _reset_barrier, _reset_witness, _reset_reservation):
        scope = self._scope(owner)
        if action == "reset_commit" and self._guarded_v17:
            from services.memory.nuke_coordinator import MemoryResetClaim
            if (type(_reset_capability) is not MemoryResetClaim
                    or memory_owner(_reset_capability.owner) != scope.owner):
                raise MemoryRequestRejectedError("host reset claim unavailable")
            _reset_capability.assert_current(owner=_reset_capability.owner,
                account_id=_reset_capability.account_id, provider=self,
                operation_id=_reset_capability.operation_id)
            paired_ticket = self._paired_review_ticket if 'memories' in components else None
            pending=self._explicit_add_review_ticket
            if type(pending) is dict:self._retire_explicit_add_noop(pending)
            imported=self._import_review_ticket
            if type(imported) is dict and type(imported.get('import_no_effect')) is dict:self._import_review_ticket=None
            if {'memories','ingest'}.intersection(components) and self._import_review_ticket is not None:
                raise MemoryRequestRejectedError('import review outcome requires reconciliation')
            if 'memories' in components and self._explicit_add_review_ticket is not None:
                # Idle transport is not proof of the original outcome.  The
                # existing original/status owner must settle it before reset.
                raise MemoryRequestRejectedError('explicit Add review outcome requires reconciliation')
            if paired_ticket is not None:
                identity=paired_ticket.get('paired_identity') if type(paired_ticket) is dict else None
                if (type(identity) is not dict or identity.get('owner')!=scope.owner or identity.get('account_id')!=_reset_capability.account_id
                    or paired_ticket.get('transport') is not self._owned_transport or paired_ticket.get('generation') is not self._owned_generation
                    or self._owned_poisoned or self._owner_rpc_busy or self._bounded_write_reserved or self._requests is None or not self._requests.empty()
                    or not self._owned_transport.current(self._owned_generation)):
                    raise MemoryRequestRejectedError('paired review owner has not settled')
            queued_retired = False
            queued_checked = False
            if {'memories','ingest'}.intersection(components):
                from src.openclank.conversation_archive import QueuedResetCompletion
                if (type(_queued_retirement) is not QueuedResetCompletion or _queued_retirement.claim is not _reset_capability
                    or _queued_retirement.provider is not self or _queued_retirement.owner != _reset_capability.owner
                    or _queued_retirement.account_id != _reset_capability.account_id
                    or _queued_retirement.operation_id != _reset_capability.operation_id):
                    raise MemoryRequestRejectedError('queued reset retirement unavailable')
                await asyncio.to_thread(_queued_retirement.assert_current)
                queued_checked = True
                queued_retired = json.loads(_queued_retirement.expected_json).get('count',0) > 0
                if queued_retired:
                    if _reset_closure is None: raise MemoryRequestRejectedError('stopped queued holder unavailable')
                    pool=_reset_closure.lease.authority._supervisor
                    await pool.validate_queued_reset_closure(_reset_closure,claim=_reset_capability)
            from src.openclank.account_request_barrier import AccountRequestBarrier
            if (type(_reset_frozen) is not dict or type(_reset_barrier) is not AccountRequestBarrier
                or getattr(_reset_capability._coordinator._reset_state,'account_request_barrier',None) is not _reset_barrier
                or not _reset_barrier.reset_witness_current(_reset_witness)):
                raise MemoryRequestRejectedError('original reset custody unavailable')
            def reset_issue_current():
                try:
                    return (getattr(_reset_capability._coordinator._reset_state,'account_request_barrier',None) is _reset_barrier
                        and _reset_barrier.reset_witness_current(_reset_witness)
                        and _reset_capability.assert_current_post_dispatch(provider=self,frozen=_reset_frozen) is True)
                except Exception:return False
            if not reset_issue_current(): raise MemoryRequestRejectedError('original reset authority unavailable')
            result = await self._host_candidate_request(scope.owner, scope.workspace_id, {
                "phase":"guarded_reset_commit", "version":1, "owner":scope.owner,
                "workspace_id":scope.workspace_id, "operation_id":_reset_capability.operation_id,
                "account_id":_reset_capability.account_id, "components":list(components),
                "expected_counts":expected_counts,
                **({'queued_retired':True} if queued_retired else {}),
            }, require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),
                _reset_original=(_reset_capability,_reset_frozen,reset_issue_current,_reset_reservation,_reset_barrier,_reset_witness),
                reset_feature=(_reset_closure.lease.reset_transport_witness if queued_retired else
                    _reset_capability._terminal_s08_witness[0] if _reset_capability._terminal_s08_witness is not None else False),
                terminal_feature=_reset_capability._terminal_s08_witness)
            from src.openclank.fm_reset_receipt import read_committed_reset_receipt
            from src.memery_external_sources import _queued_owned_thread
            def reset_current():
                try:
                    return (getattr(_reset_capability._coordinator._reset_state,'account_request_barrier',None) is _reset_barrier
                        and _reset_barrier.reset_witness_current(_reset_witness)
                        and _reset_capability.assert_current_post_dispatch(provider=self,frozen=_reset_frozen) is True
                        and (not queued_retired or (pool.queued_reset_closure_current(_reset_closure,claim=_reset_capability)
                            and _reset_closure.lease.barrier is _reset_barrier
                            and _reset_closure.lease.witness is _reset_witness
                            and _reset_closure.lease.profile.assert_current() is None)))
                except Exception:return False
            original=await _queued_owned_thread(read_committed_reset_receipt,self._guarded_preparation,_reset_frozen,current=reset_current)
            if original.get('sql_result')!=result:
                raise MemoryRequestRejectedError('original reset result changed')
            await self._host_guarded_reset_stamp(claim=_reset_capability,frozen=_reset_frozen,receipt=original,current=reset_current)
            if queued_retired:
                await _queued_owned_thread(self._assert_reset_retirement_ready,_reset_closure.lease.reset_transport_witness)
            if _reset_capability._terminal_s08_witness is not None:
                await _queued_owned_thread(self._assert_terminal_s08_ready,_reset_capability._terminal_s08_witness)
            if queued_checked and not queued_retired:
                await asyncio.to_thread(_queued_retirement.assert_current)
            if queued_retired:
                frozen=getattr(_reset_closure.lease,'reset_native_frozen',None)
                await pool.validate_post_dispatch_queued_reset(_reset_closure,claim=_reset_capability,frozen=frozen)
            if isinstance(result, dict) and result.get("complete") is True and "memories" in components:
                _reset_capability.assert_current(owner=_reset_capability.owner,account_id=_reset_capability.account_id,provider=self,operation_id=_reset_capability.operation_id)
                if paired_ticket is not None:
                    if (self._paired_review_ticket is not paired_ticket or paired_ticket.get('transport') is not self._owned_transport
                        or paired_ticket.get('generation') is not self._owned_generation or self._owned_poisoned or self._owner_rpc_busy
                        or self._bounded_write_reserved or not self._owned_transport.current(self._owned_generation)):
                        raise MemoryRequestRejectedError('paired review reset custody changed')
                    self._paired_review_ticket=None
                def current():
                    try: pool.assert_post_dispatch_queued_reset(_reset_closure,claim=_reset_capability,frozen=frozen);return True
                    except Exception:return False
                await self._delete_principal_bindings(scope.owner,current=current if queued_retired else None)
            return result
        arguments: Dict[str, Any] = {
            "action": action,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
            "components": list(components),
        }
        if expected_counts is not None:
            arguments["expected_counts"] = expected_counts
        result = await self._call_tool("owner_lifecycle", arguments)
        if (
            action == "reset_commit"
            and isinstance(result, dict)
            and result.get("complete") is True
            and "memories" in components
        ):
            await self._delete_principal_bindings(scope.owner)
        return result

    def _seal_nonselective_reset_unsent(self, *, claim, frozen):
        """Produce only a positively unissued, actually drained original fact."""
        ticket=self._reset_handoff_state.get('original')
        if (type(ticket) is not dict or ticket.get('reset_frozen')!=frozen
            or getattr(ticket.get('reset_claim'),'_coordinator',None) is not getattr(claim,'_coordinator',None)
            or getattr(ticket.get('reset_claim'),'operation_id',None)!=getattr(claim,'operation_id',None)
            or ticket.get('issued') is not False or ticket.get('may_issue') is not False
            or ticket.get('reset_unsent_drained') is not True or ticket.get('reset_reissued') is True
            or ticket.get('reset_future') is None or not ticket['reset_future'].done()):
            raise MemoryRequestRejectedError('original unsent fact unavailable')
        claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
        fact=ticket.get('reset_unsent_fact')
        if fact is None:
            fact=_OriginalResetUnsent(_ORIGINAL_RESET_UNSENT_SEAL,self,ticket);ticket['reset_unsent_fact']=fact
        return fact

    def _assert_nonselective_reset_unsent(self, fact, claim, frozen):
        ticket=self._reset_handoff_state.get('original')
        if (type(fact) is not _OriginalResetUnsent or fact._seal is not _ORIGINAL_RESET_UNSENT_SEAL
            or fact.provider is not self or type(ticket) is not dict or fact.token is not ticket
            or ticket.get('reset_unsent_fact') is not fact or ticket.get('reset_frozen')!=frozen
            or ticket.get('issued') is not False or ticket.get('may_issue') is not False
            or ticket.get('reset_unsent_drained') is not True or not ticket['reset_future'].done()
            or getattr(ticket.get('reset_claim'),'_coordinator',None) is not getattr(claim,'_coordinator',None)
            or getattr(ticket.get('reset_claim'),'operation_id',None)!=claim.operation_id):
            raise MemoryRequestRejectedError('original unsent fact unavailable')
        claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=self,operation_id=claim.operation_id)
        return True

    def _retire_nonselective_reset_unsent(self, fact, claim, preview):
        """Drop the same fact only after the exact preview is durable again."""
        from services.memory.skill_lifecycle import locked
        ticket=self._reset_handoff_state.get('original')
        if (type(fact) is not _OriginalResetUnsent or fact._seal is not _ORIGINAL_RESET_UNSENT_SEAL
            or fact.provider is not self or type(ticket) is not dict or fact.token is not ticket
            or ticket.get('reset_unsent_fact') is not fact or ticket.get('issued') is not False
            or ticket.get('reset_unsent_drained') is not True or type(preview) is not dict
            or getattr(ticket.get('reset_claim'),'_coordinator',None) is not getattr(claim,'_coordinator',None)
            or getattr(ticket.get('reset_claim'),'operation_id',None)!=getattr(claim,'operation_id',None)):
            raise MemoryRequestRejectedError('original unsent fact changed')
        with locked(os.fspath(claim._coordinator.lock_target)):
            if claim._coordinator._load_journal().get(claim.operation_id)!=preview:
                raise MemoryRequestRejectedError('original reset withdrawal changed')
        if self._reset_handoff_state.get('original') is not ticket:
            raise MemoryRequestRejectedError('original unsent fact changed')
        self._reset_handoff_state['original']=None

    def _original_reset_io_settled(self, claim, frozen):
        ticket=self._reset_handoff_state.get('original')
        if (type(ticket) is not dict or ticket.get('reset_frozen')!=frozen
            or getattr(ticket.get('reset_claim'),'_coordinator',None) is not getattr(claim,'_coordinator',None)
            or getattr(ticket.get('reset_claim'),'operation_id',None)!=getattr(claim,'operation_id',None)
            or getattr(ticket.get('reset_claim'),'account_id',None)!=getattr(claim,'account_id',None)):
            return False
        old=ticket.get('transport'); generation=ticket.get('generation'); process=ticket.get('reset_process'); task=ticket.get('reset_owner_task')
        if old is None or process is None or task is None or old.process is not process:
            return False
        if ticket.get('reset_io_settled') is True:
            return True
        confirmation=old.exit_confirmation(generation)
        return bool(task.done() and confirmation.done() and not confirmation.cancelled()
            and confirmation.exception() is None and type(confirmation.result()) is int
            and process.returncode==confirmation.result())

    async def _host_guarded_reset_stamp(self, *, claim, frozen, receipt, current, _reset_handoff_token=None):
        """Adopt only this positively committed original reset's Native stamp."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.memery_database import GuardedStorePreparation
        from src.openclank.fm_reset_receipt import read_committed_reset_receipt
        from src.memery_external_sources import _queued_owned_thread
        if (_reset_handoff_token is None and not self._original_reset_io_settled(claim,frozen)):
            raise MemoryRequestRejectedError('original reset I/O unsettled')
        if (type(claim) is not MemoryResetClaim or type(frozen) is not dict or set(frozen)!={'version','database','request'}
            or type(frozen.get('request')) is not dict or type(receipt) is not dict or receipt.get('state')!='committed'
            or receipt.get('cleanup')!='unproved' or not callable(current) or current() is not True):
            raise MemoryRequestRejectedError('original reset stamp unavailable')
        prepared=self._guarded_preparation;transport=self._owned_transport;generation=self._owned_generation;task=self._owner_task
        if (type(prepared) is not GuardedStorePreparation or transport is None or transport.process is None
            or task is None or task.done() or not transport.current(generation)):
            raise MemoryRequestRejectedError('original reset stamp unavailable')
        process=transport.process;request=json.loads(json.dumps(frozen['request'],ensure_ascii=False,allow_nan=False))
        def check():
            try:
                return (current() is True and claim.assert_current_post_dispatch(provider=self,frozen=frozen) is True
                    and self._guarded_preparation is prepared and self._owned_transport is transport
                    and self._owned_generation is generation and self._owner_task is task and not task.done()
                    and transport.process is process and process.returncode is None and transport.current(generation))
            except Exception:return False
        if not check() or await _queued_owned_thread(read_committed_reset_receipt,prepared,frozen,current=check)!=receipt:
            raise MemoryRequestRejectedError('original reset receipt changed')
        fact=await self._host_candidate_request(request['owner'],request['workspace_id'],
            {'phase':'guarded_reset_stamp',**request},require_live=True,_reset_handoff_token=_reset_handoff_token)
        if not check(): raise MemoryRequestRejectedError('original reset authority changed')
        try:
            await _queued_owned_thread(prepared._accept_owned_reset_stamp,fact,frozen=frozen,receipt=receipt,claim=claim,provider=self,current=check)
        except (OSError,ValueError,sqlite3.Error) as exc:
            raise MemoryRequestRejectedError('original reset stamp unavailable') from exc
        if not check(): raise MemoryRequestRejectedError('original reset authority changed')
        return fact

    def _ack_original_reset_io(self, *, claim, frozen, result):
        from services.memory.skill_lifecycle import locked
        ticket=self._reset_handoff_state.get('original')
        if (type(result) is not dict or claim.assert_current_post_dispatch(provider=self,frozen=frozen) is not True):
            raise MemoryRequestRejectedError('original reset acknowledgement unavailable')
        with locked(os.fspath(claim._coordinator.lock_target)):
            row=claim._coordinator._load_journal().get(claim.operation_id)
            if (type(row) is not dict or row.get('reset_native_frozen')!=frozen
                or row.get('reset_native_result')!=result):
                raise MemoryRequestRejectedError('original reset acknowledgement changed')
        if self._reset_handoff_state.get('original') is not ticket:
            raise MemoryRequestRejectedError('original reset acknowledgement changed')
        if ticket is None: return
        if not self._original_reset_io_settled(claim,frozen):
            raise MemoryRequestRejectedError('original reset acknowledgement unavailable')
        self._reset_handoff_state['original']=None

    async def _host_guarded_reset_cleanup_no_closure(self, *, claim, frozen, receipt, barrier, witness):
        """Finish one positively committed zero-queue original under its drain witness."""
        from services.memory.nuke_coordinator import MemoryResetClaim
        from src.openclank.account_request_barrier import AccountRequestBarrier
        from src.openclank.fm_reset_receipt import read_committed_reset_receipt
        from src.memery_external_sources import _queued_owned_thread
        if (type(claim) is not MemoryResetClaim or type(barrier) is not AccountRequestBarrier
            or type(frozen) is not dict or type(frozen.get('request')) is not dict
            or frozen['request'].get('queued_retired') is True or type(receipt) is not dict
            or receipt.get('state')!='committed' or receipt.get('cleanup')!='unproved'):
            raise MemoryRequestRejectedError('original reset cleanup unavailable')
        def current():
            try:
                return (getattr(claim._coordinator._reset_state,'account_request_barrier',None) is barrier
                    and barrier.reset_witness_current(witness)
                    and claim.assert_current_post_dispatch(provider=self,frozen=frozen) is True)
            except Exception:return False
        if not current(): raise MemoryRequestRejectedError('original reset authority changed')
        await self._host_guarded_reset_stamp(claim=claim,frozen=frozen,receipt=receipt,current=current)
        request=frozen['request'];db=frozen['database']
        payload={'phase':'guarded_reset_cleanup',**request,'path':db['path'],'device':db['device'],
            'inode':db['inode'],'database_id':db['database_id']}
        if not current(): raise MemoryRequestRejectedError('original reset authority changed')
        response=await self._host_candidate_request(request['owner'],request['workspace_id'],payload,
            require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),before_issue=current)
        if response!={'state':'cleaned'} or not current():
            raise MemoryRequestRejectedError('original reset cleanup unavailable')
        if 'memories' in request['components']:
            await self._delete_principal_bindings(request['owner'],current=current)
        if (not current() or await _queued_owned_thread(read_committed_reset_receipt,self._guarded_preparation,frozen,current=current)!=receipt
            or not current()):
            raise MemoryRequestRejectedError('original reset receipt changed')
        return receipt['sql_result']

    async def _host_guarded_reset_cleanup(self, *, claim, closure, frozen, receipt):
        """Finish only exact committed reset cleanup on the current owned child."""
        from src.openclank.fm_reset_receipt import read_committed_reset_receipt
        from src.openclank.mimo_supervisor import _queued_owned_thread
        pool=closure.lease.authority._supervisor
        if (type(frozen) is not dict or type(receipt) is not dict or receipt.get('state')!='committed'
            or receipt.get('cleanup')!='unproved' or type(receipt.get('sql_result')) is not dict
            or not self._host_bounded_write_available()):
            raise MemoryRequestRejectedError('committed reset cleanup transport unavailable')
        def pre_current():
            try:
                return (pool.queued_reset_closure_current(closure,claim=claim)
                    and closure.lease.barrier.reset_witness_current(closure.lease.witness)
                    and closure.lease.profile.assert_current() is None
                    and claim.assert_current_post_dispatch(provider=self,frozen=frozen) is True)
            except Exception:return False
        await self._host_guarded_reset_stamp(claim=claim,frozen=frozen,receipt=receipt,current=pre_current)
        await pool.validate_post_dispatch_queued_reset(closure,claim=claim,frozen=frozen)
        def current():
            try: pool.assert_post_dispatch_queued_reset(closure,claim=claim,frozen=frozen);return True
            except Exception:return False
        observed=await _queued_owned_thread(read_committed_reset_receipt,self._guarded_preparation,frozen,current=current)
        if observed!=receipt: raise MemoryRequestRejectedError('committed reset receipt changed')
        request=frozen['request'];db=frozen['database']
        payload={'phase':'guarded_reset_cleanup',**request,'path':db['path'],'device':db['device'],
            'inode':db['inode'],'database_id':db['database_id']}
        response=await self._host_candidate_request(request['owner'],request['workspace_id'],payload,
            require_live=True,write_deadline=float(asyncio.get_running_loop().time()+30.0),reset_feature=closure.lease.reset_transport_witness,before_issue=current)
        if response!={'state':'cleaned'}: raise MemoryRequestRejectedError('committed reset cleanup unavailable')
        await pool.validate_post_dispatch_queued_reset(closure,claim=claim,frozen=frozen)
        if 'memories' in request['components']:
            await self._delete_principal_bindings(request['owner'],current=current)
        observed=await _queued_owned_thread(read_committed_reset_receipt,self._guarded_preparation,frozen,current=current)
        if observed!=receipt: raise MemoryRequestRejectedError('committed reset receipt changed')
        await pool.validate_post_dispatch_queued_reset(closure,claim=claim,frozen=frozen)
        return receipt['sql_result']

    async def _delete_principal_bindings(self, owner: str, *, current=None) -> None:
        """Drop the owner's Python-side principal bindings after a reset/purge.

        ``fm_v2_principal_bindings`` is created by the Python principal
        bootstrap and unknown to the Rust schema, so the store's owner-scoped
        delete closure cannot know it. Left behind, a stale binding would
        resurrect references to entities the reset just erased.
        """
        db_path = self._local_db_path()
        if db_path is None:
            if current is not None: raise MemoryRequestRejectedError('committed reset cleanup unavailable')
            return

        def _delete() -> None:
            import sqlite3

            with connect_writer(db_path, timeout=30) as conn:
                if current is not None and current() is not True:
                    raise MemoryRequestRejectedError('committed reset cleanup authority changed')
                exists = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='fm_v2_principal_bindings')"
                ).fetchone()
                if not exists or not exists[0]:
                    return
                conn.execute(
                    "DELETE FROM fm_v2_principal_bindings WHERE owner_id=?",
                    (owner,),
                )
                if current is not None and current() is not True:
                    raise MemoryRequestRejectedError('committed reset cleanup authority changed')

        await asyncio.to_thread(_delete)

    async def rename_owner(self, new_owner: str, *, owner: Optional[str] = None, _source_capability=None) -> Dict[str, Any]:
        scope = self._scope(owner)
        if self._guarded_v17:
            from src.openclank.chat_source_bridge import HostAccountSourceTransition
            if type(_source_capability) is not HostAccountSourceTransition:
                raise MemoryRequestRejectedError('host account source transition unavailable')
            _source_capability.assert_current(provider=self,action='rename',owner=scope.owner,new_owner=new_owner)
        return await self._call_tool(
            "owner_lifecycle",
            {
                "action": "rename",
                "owner": scope.owner,
                "workspace_id": scope.workspace_id,
                "new_owner": new_owner,
            },
        )

    async def groom(
        self,
        op: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        scope = MemoryScope(
            owner=owner or "",
            workspace_id=workspace_id or self._workspace_id,
            workspace_path=self._workspace_id,
        )
        args: Dict[str, Any] = {
            "op": op,
            "dry_run": dry_run,
            "owner": scope.owner,
            "workspace_id": scope.workspace_id,
        }
        return await self._call_tool("groom", args)

    async def retention(
        self,
        action: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        **policy: Any,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        args: Dict[str, Any] = {
            "action": action,
            "owner": scope.owner,
            "workspace_id": workspace_id or scope.workspace_id,
            **policy,
        }
        return await self._call_tool("memory_retention", args)

    async def forget(
        self,
        action: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        selector_kind: Optional[str] = None,
        selector: Optional[str] = None,
        preview_token: Optional[str] = None,
        tombstone_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        args: Dict[str, Any] = {
            "action": action,
            "owner": scope.owner,
            "workspace_id": workspace_id or scope.workspace_id,
        }
        for key, value in {
            "selector_kind": selector_kind,
            "selector": selector,
            "preview_token": preview_token,
            "tombstone_id": tombstone_id,
            "operation_id": operation_id,
        }.items():
            if value is not None:
                args[key] = value
        return await self._call_tool("memory_forget", args)

    async def export_scope(
        self,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        scope = self._scope(owner)
        return await self._call_tool(
            "memory_export",
            {
                "owner": scope.owner,
                "workspace_id": workspace_id or scope.workspace_id,
            },
        )

    async def explain(
        self,
        memory_id: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        scope = self._scope(owner)
        result = await self._call_tool(
            "memory_explain",
            {
                "id": memory_id,
                "owner": scope.owner,
                "workspace_id": workspace_id or scope.workspace_id,
            },
        )
        explanation = result.get("explanation")
        return dict(explanation) if isinstance(explanation, dict) else None
