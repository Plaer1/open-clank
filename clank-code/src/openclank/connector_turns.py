"""Existing TUI receipt authority joined to scoped connector chat starts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError
from core.database import Session, ChatMessage, TuiTurnSubmission, get_db_session


class ConnectorTurnConflict(ValueError):
    pass


@dataclass(frozen=True)
class ConnectorTurnGuard:
    owner: str
    account: str
    session_id: str
    workspace_id: str
    endpoint_id: str
    model: str
    route_id: str
    expected_revision: int
    submission_id: str
    request_hash: str
    root_id: str
    run_id: str
    current: object

    def prepare(self, db, row, message):
        """Run under SQLite BEGIN IMMEDIATE in the message's own transaction."""
        receipt = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == self.submission_id).first()
        if (not callable(self.current) or self.current() is not True or row is None
                or row.id != self.session_id or row.owner != self.owner or row.archived
                or row.workspace_id != self.workspace_id or row.endpoint_id != self.endpoint_id
                or row.model != self.model or row.provider_model_route_id != self.route_id
                or row.transcript_revision != self.expected_revision
                or receipt is None or receipt.owner != self.owner or receipt.session_id != self.session_id
                or receipt.request_hash != self.request_hash or receipt.state != 'submitting'
                or message.role != 'user' or message.persistence_id != self.root_id):
            raise ConnectorTurnConflict('Session, credential or transcript changed before commit')
        receipt.state = 'committed'

    def before_commit(self):
        if not callable(self.current) or self.current() is not True:
            raise ConnectorTurnConflict('Credential changed before commit')

    def assert_dispatch(self, *, bridge, lease, state, native_session, wire):
        """Revalidate the original connector and actual managed binding at prompt issue."""
        try:
            self._assert_dispatch(bridge=bridge, lease=lease, state=state, native_session=native_session, wire=wire)
        except ConnectorTurnConflict:
            raise
        except Exception as exc:
            raise ConnectorTurnConflict('Connector authority unavailable') from exc

    def _assert_dispatch(self, *, bridge, lease, state, native_session, wire):
        from src.openclank.mimo_supervisor import AgentWorkerLease
        from src.openclank.chat_routing import resolve_chat_route
        from src.openclank.transcript_projection import get_managed_binding
        if (type(lease) is not AgentWorkerLease or lease._released or lease.owner != self.owner
                or lease.worker.bridge is not bridge or not lease.worker.is_alive()
                or lease._pool.worker_for_owner(self.owner) is not lease.worker
                or lease._pool._owner_lifecycle_epoch(self.owner) != lease._owner_epoch
                or lease.generation != getattr(lease.worker, 'installed_generation', None)
                or state.owner_account_id != self.account or state.root_operation_id != self.root_id
                or getattr(bridge, '_owner', None) != self.owner
                or not callable(state.original_credential_current)
                or state.original_credential_current() is not True or self.current() is not True):
            raise ConnectorTurnConflict('Connector runtime or credential changed')
        with get_db_session() as db:
            row = db.query(Session).filter(Session.id == self.session_id, Session.owner == self.owner).first()
            receipt = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == self.submission_id).first()
            message = db.query(ChatMessage).filter(ChatMessage.id == self.root_id, ChatMessage.session_id == self.session_id).first()
            if (row is None or row.archived or row.workspace_id != self.workspace_id
                    or row.endpoint_id != self.endpoint_id or row.model != self.model
                    or row.provider_model_route_id != self.route_id or receipt is None
                    or receipt.owner != self.owner or receipt.session_id != self.session_id
                    or receipt.request_hash != self.request_hash or receipt.state != 'committed'
                    or message is None or message.role != 'user'):
                raise ConnectorTurnConflict('Connector session or receipt changed')
        route = resolve_chat_route(owner=self.owner, endpoint_id=self.endpoint_id,
                                   model_id=self.model, model_route_id=self.route_id)
        binding = get_managed_binding(self.session_id, owner=self.owner)
        if (binding.get('owner') != self.owner or binding.get('stableChatID') != self.session_id
                or binding.get('engineSessionID') != native_session
                or binding.get('authorityWorkspaceID') != self.workspace_id
                or binding.get('transition') is not None or type(wire) is not dict
                or wire.get('rootOperationID') != self.root_id or wire.get('modelRouteID') != self.route_id
                or wire.get('connectionID') != route.connection_id
                or wire.get('modelID') != route.provider_model_id
                or self.current() is not True):
            raise ConnectorTurnConflict('Connector managed binding changed')


def connector_send_available(state) -> bool:
    """Structural availability only; never starts or probes an engine."""
    from src.openclank.managed_protocol import MANAGED_METHODS
    from src.openclank.acp_bridge import ACPBridge
    supervisor = getattr(state, 'mimo_supervisor', None)
    auth = getattr(state, 'auth_manager', None)
    return bool('_openclank/session/v1/dispatch/admit' in MANAGED_METHODS
        and callable(getattr(ACPBridge, '_handle_connector_dispatch_admit', None))
        and callable(getattr(state, 'openclank_chat_stream_handler', None))
        and callable(getattr(state, 'capture_original_credential', None))
        and callable(getattr(auth, 'account_id', None))
        and callable(getattr(auth, 'username_for_account_id', None))
        and callable(getattr(supervisor, 'admit_agent', None)))


def _identity(account: str, key: str) -> tuple[str, str, str]:
    digest = hashlib.sha256(('connector-turn-v1\0' + account + '\0' + key).encode()).hexdigest()
    return digest, 'root_' + digest[:32], hashlib.sha256(('connector-run-v1\0' + digest).encode()).hexdigest()[:32]


def _outcome(row, root_id: str, run_id: str) -> dict:
    from src import agent_runs
    session_id = row['session_id'] if isinstance(row, dict) else row.session_id
    receipt_state = row['state'] if isinstance(row, dict) else row.state
    run = agent_runs.get_run(session_id)
    state = 'unknown'
    if receipt_state == 'rejected': state = 'rejected'
    elif receipt_state == 'committed' and run is not None and run.run_id == run_id:
        state = 'active' if agent_runs.is_active(session_id) else ('done' if run.status == 'done' else 'unknown')
    return {'session_id':session_id, 'root_operation_id':root_id, 'run_id':run_id, 'state':state}


async def submit_connector_turn(request: Request, session_id: str, body: dict, identity) -> dict:
    from src import agent_runs
    if (type(body) is not dict or set(body) != {'operation_key','message','expected_revision','workspace_id','endpoint_id','model'}
            or any(not isinstance(body[k], str) or not body[k] or body[k] != body[k].strip()
                   for k in ('operation_key','workspace_id','endpoint_id','model'))
            or len(body['operation_key']) > 100 or not isinstance(body['message'], str) or len(body['message']) > 200000
            or not body['message'].strip() or any(len(body[k]) > 512 for k in ('workspace_id','endpoint_id','model'))
            or type(body['expected_revision']) is not int or body['expected_revision'] < 0):
        raise HTTPException(400, 'Explicit operation, text, workspace, provider, model and revision required')
    submission_id, root_id, run_id = _identity(identity.account_id, body['operation_key'])
    canonical = json.dumps([identity.account_id, session_id, body], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    request_hash = hashlib.sha256(canonical).hexdigest()
    key = 'connector:' + body['operation_key']
    def existing_or_create():
        with get_db_session() as db:
            row = db.query(Session).filter(Session.id == session_id, Session.owner == identity.owner).first()
            if row is None: raise HTTPException(404, 'Session not found')
            prior = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == submission_id).first()
            if prior is not None:
                if prior.owner != identity.owner or prior.session_id != session_id or prior.request_hash != request_hash:
                    raise HTTPException(409, 'Operation key already used for another request')
                return {'session_id':prior.session_id, 'state':prior.state}, None
            if not connector_send_available(request.app.state):
                raise HTTPException(503, 'Connector turn start is unavailable')
            if (row.archived or row.workspace_id != body['workspace_id'] or row.endpoint_id != body['endpoint_id']
                    or row.model != body['model'] or not row.provider_model_route_id):
                raise HTTPException(409, 'Session target changed')
            if row.transcript_revision != body['expected_revision']:
                raise HTTPException(409, 'Transcript revision changed')
            if agent_runs.is_active(session_id): raise HTTPException(409, 'Session already has an active turn')
            if identity.current() is not True: raise HTTPException(401, 'Credential no longer current')
            guard = ConnectorTurnGuard(identity.owner, identity.account_id, session_id, row.workspace_id, row.endpoint_id,
                row.model, row.provider_model_route_id, body['expected_revision'], submission_id, request_hash, root_id, run_id, identity.current)
            receipt = TuiTurnSubmission(id=submission_id, owner=identity.owner, session_id=session_id,
                idempotency_key=key, request_hash=request_hash, state='submitting')
            db.add(receipt)
            db.flush()
            return {'session_id':session_id, 'state':'submitting'}, guard
    try:
        row, guard = existing_or_create()
    except IntegrityError:
        with get_db_session() as db:
            row = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == submission_id).first()
            if row is None or row.owner != identity.owner or row.session_id != session_id or row.request_hash != request_hash:
                raise HTTPException(409, 'Operation key already used for another request')
            row = {'session_id':row.session_id, 'state':row.state}
            guard = None
    if guard is None:
        if identity.current() is not True: raise HTTPException(401, 'Credential no longer current')
        return _outcome(row, root_id, run_id)
    handler = getattr(request.app.state, 'openclank_chat_stream_handler', None)
    if not callable(handler): return _outcome(row, root_id, run_id)
    from routes.tui_routes import _json_request_for_chat
    synthetic = await _json_request_for_chat(request, {'session':session_id, 'message':body['message'],
        'attachments':[], 'allow_bash':False, 'allow_web_search':False, 'mode':'agent'})
    synthetic.state.connector_turn_guard = guard
    try:
        response = await handler(synthetic)
    except ConnectorTurnConflict as exc:
        with get_db_session() as db:
            current_row = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == submission_id).first()
            if current_row is not None and current_row.state == 'submitting': current_row.state = 'rejected'
        raise HTTPException(409, str(exc)) from exc
    except HTTPException as exc:
        rejected = False
        if exc.status_code < 500:
            with get_db_session() as db:
                current_row = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == submission_id).first()
                if current_row is not None and current_row.state == 'submitting':
                    current_row.state = 'rejected'
                    rejected = True
        if rejected: raise exc
        return {'session_id':session_id, 'root_operation_id':root_id, 'run_id':run_id, 'state':'unknown'}
    except Exception:
        return {'session_id':session_id, 'root_operation_id':root_id, 'run_id':run_id, 'state':'unknown'}
    finally:
        try: del synthetic.state.connector_turn_guard
        except AttributeError: pass
    with get_db_session() as db:
        current_row = db.query(TuiTurnSubmission).filter(TuiTurnSubmission.id == submission_id).first()
        if current_row is None: raise HTTPException(409, 'Submission outcome unavailable')
        result = _outcome(current_row, root_id, run_id)
    if response.headers.get('X-Agent-Run-ID') != run_id: result['state'] = 'unknown'
    return result
