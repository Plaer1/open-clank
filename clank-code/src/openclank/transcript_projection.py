"""Canonical transcript revision and active Open Clank agent projection records."""

from __future__ import annotations

import hashlib
import json
import os
import hmac
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.exc import OperationalError

from core.database import ChatMessage, MimoProjection, Session, SessionLocal


_EXPECTED_UNSET = object()


def make_current_input_owner_receipt(owner: str, account_id: str, chat_id: str, root_operation_id: str, *, theme_status_only: bool = False, code_activity_workspace_id: str | None = None) -> dict:
    """Seal one authenticated host input; this is not a claim about chat history."""
    from src.secret_storage import keyed_digest
    payload = {'version': 1, 'owner': owner, 'accountID': account_id,
               'chatID': chat_id, 'rootOperationID': root_operation_id, 'themeStatusOnly': theme_status_only}
    if code_activity_workspace_id is not None:
        if type(code_activity_workspace_id) is not str or not code_activity_workspace_id.strip() or len(code_activity_workspace_id)>512:
            raise ValueError('code activity workspace unavailable')
        payload['codeActivityWorkspaceID'] = code_activity_workspace_id
    if type(theme_status_only) is not bool or any(type(payload[key]) is not str or not payload[key] or len(payload[key]) > 512
           for key in ('owner', 'accountID', 'chatID', 'rootOperationID')):
        raise ValueError('current input owner unavailable')
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'payload': payload, 'mac': keyed_digest(raw, context='openclank.c2.current-input-owner.v1')}


def verify_current_input_owner_receipt(value: object, *, owner: str, chat_id: str, root_operation_id: str) -> str | None:
    from src import secret_storage
    if not secret_storage._KEY_PATH.is_file() or type(value) is not dict or set(value) != {'payload', 'mac'}:
        return None
    payload, mac = value['payload'], value['mac']
    keys = {'version','owner','accountID','chatID','rootOperationID','themeStatusOnly'}
    if (type(payload) is not dict or set(payload) not in (keys, keys | {'codeActivityWorkspaceID'})
            or payload.get('version') != 1 or type(payload.get('themeStatusOnly')) is not bool or payload.get('owner') != owner or payload.get('chatID') != chat_id
            or ('codeActivityWorkspaceID' in payload and (type(payload['codeActivityWorkspaceID']) is not str or not payload['codeActivityWorkspaceID'].strip() or len(payload['codeActivityWorkspaceID'])>512))
            or payload.get('rootOperationID') != root_operation_id or type(payload.get('accountID')) is not str
            or not payload['accountID'] or len(payload['accountID']) > 512 or type(mac) is not str):
        return None
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        expected = secret_storage.keyed_digest(raw, context='openclank.c2.current-input-owner.v1')
        return payload['accountID'] if hmac.compare_digest(mac, expected) else None
    except (ValueError, TypeError, OSError):
        return None


def verify_current_input_theme_status(value: object, *, owner: str, chat_id: str, root_operation_id: str) -> bool:
    if not verify_current_input_owner_receipt(value, owner=owner, chat_id=chat_id, root_operation_id=root_operation_id):
        return False
    return value['payload']['themeStatusOnly'] is True


def verify_current_input_code_activity(value: object, *, owner: str, chat_id: str, root_operation_id: str) -> str | None:
    if not verify_current_input_owner_receipt(value, owner=owner, chat_id=chat_id, root_operation_id=root_operation_id):
        return None
    return value['payload'].get('codeActivityWorkspaceID')


def _managed_receipt_key(identity: dict) -> str:
    raw = json.dumps([identity['sessionID'], identity['actorID']], ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def _managed_receipt_binding(state: dict, receipt: dict) -> bool:
    binding = state.get('managed_binding')
    return (isinstance(binding, dict) and binding.get('owner') == receipt['owner']
        and binding.get('stableChatID') == receipt['chatID'] and binding.get('engineSessionID') == receipt['rootSessionID']
        and binding.get('workspaceRevision') == receipt['workspaceRevision']
        and binding.get('mappingRevision') == receipt['mappingRevision']
        and binding.get('authorityWorkspaceID') == receipt['authorityWorkspaceID'])


def _managed_receipt_mac(receipt: dict, *, create_key: bool = False) -> str:
    from src import secret_storage
    if not create_key and not secret_storage._KEY_PATH.is_file():
        raise ValueError('managed turn receipt key unavailable')
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return secret_storage.keyed_digest(encoded, context='openclank.c2.managed-turn-receipt.v1')


def record_managed_turn_receipt(receipt: dict) -> None:
    """Commit a content-free host turn result against the exact current chat binding."""
    fields = {'version','owner','accountID','chatID','holderID','rootSessionID','sessionID','rootOperationID',
              'actorID','executionRevision','innerTurnID','outcome','workspaceRevision','mappingRevision','authorityWorkspaceID'}
    text_fields = fields - {'version','executionRevision','workspaceRevision','mappingRevision','outcome','authorityWorkspaceID'}
    if (type(receipt) is not dict or set(receipt) != fields or receipt['version'] != 1
            or any(type(receipt[k]) is not str or not receipt[k] or len(receipt[k]) > 512 for k in text_fields)
            or type(receipt['authorityWorkspaceID']) is not str or len(receipt['authorityWorkspaceID']) > 512
            or any(type(receipt[k]) is not int or receipt[k] < 0 for k in ('executionRevision','workspaceRevision','mappingRevision'))
            or receipt['outcome'] not in ('completed','cancelled','error')):
        raise ValueError('managed turn receipt unavailable')
    with SessionLocal() as db:
        if db.get_bind().dialect.name == 'sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = db.query(Session).filter(Session.id == receipt['chatID'], Session.owner == receipt['owner']).with_for_update().first()
        if row is None or row.archived:
            raise ValueError('managed turn receipt chat unavailable')
        state = dict(row.mimo_state or {})
        if not _managed_receipt_binding(state, receipt): raise ValueError('managed turn receipt binding changed')
        container = state.get('managed_turn_receipts', {'version':1,'items':{}})
        if type(container) is not dict or set(container) != {'version','items'} or container['version'] != 1 or type(container['items']) is not dict:
            raise ValueError('managed turn receipt state unavailable')
        items = dict(container['items']); key = _managed_receipt_key(receipt); envelope = items.get(key)
        old = envelope.get('payload') if type(envelope) is dict and set(envelope) == {'payload','mac'} else None
        if envelope is not None and (type(old) is not dict or type(envelope['mac']) is not str
                or not hmac.compare_digest(envelope['mac'], _managed_receipt_mac(old))):
            raise ValueError('managed turn receipt authentication unavailable')
        if old == receipt: return
        if old is not None and (type(old.get('executionRevision')) is not int
                or old['executionRevision'] > receipt['executionRevision']
                or (old['executionRevision'] == receipt['executionRevision'] and old.get('innerTurnID') == receipt['innerTurnID'])):
            raise ValueError('managed turn receipt conflicts')
        if old is None and len(items) >= 4096: raise ValueError('managed turn receipt capacity unavailable')
        # Never recreate a lost key while this chat already contains receipts,
        # even when the new actor has no entry of its own.
        items[key] = {'payload':dict(receipt), 'mac':_managed_receipt_mac(receipt, create_key=old is None and not items)}
        state['managed_turn_receipts'] = {'version':1,'items':items}
        state['revision'] = int(state.get('revision') or 0) + 1
        row.mimo_state = state
        db.commit()


def read_managed_turn_receipt(chat_id: str, owner: str, identity: dict) -> dict | None:
    """Read only an exact receipt from the still-current host chat binding."""
    with SessionLocal() as db:
        row = db.query(Session).filter(Session.id == chat_id, Session.owner == owner).first()
        if row is None or row.archived: return None
        state = dict(row.mimo_state or {}); container = state.get('managed_turn_receipts')
        if type(container) is not dict or set(container) != {'version','items'} or container['version'] != 1 or type(container['items']) is not dict:
            return None
        envelope = container['items'].get(_managed_receipt_key(identity))
        if type(envelope) is not dict or set(envelope) != {'payload','mac'} or type(envelope['payload']) is not dict or type(envelope['mac']) is not str:
            return None
        receipt = envelope['payload']
        try:
            if not hmac.compare_digest(envelope['mac'], _managed_receipt_mac(receipt)): return None
        except (ValueError, TypeError, OSError): return None
        fields = ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID')
        if (type(receipt) is not dict or any(receipt.get(k) != identity[k] for k in fields)
                or receipt.get('owner') != owner or receipt.get('chatID') != chat_id): return None
        try:
            if not _managed_receipt_binding(state, receipt): return None
        except (KeyError, TypeError): return None
        return dict(receipt)


def record_managed_execution_completion(chat_id: str, owner: str, account_id: str, identity: dict, outcome: str) -> None:
    """Durably append exact execution acknowledgement to an authenticated turn receipt."""
    if outcome not in ('success','failure','cancelled') or type(account_id) is not str or not account_id:
        raise ValueError('managed execution completion unavailable')
    with SessionLocal() as db:
        if db.get_bind().dialect.name == 'sqlite': db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = db.query(Session).filter(Session.id == chat_id, Session.owner == owner).with_for_update().first()
        if row is None or row.archived: raise ValueError('managed execution chat unavailable')
        state = dict(row.mimo_state or {}); container = state.get('managed_turn_receipts')
        if type(container) is not dict or set(container) != {'version','items'} or container['version'] != 1 or type(container['items']) is not dict:
            raise ValueError('managed execution receipt unavailable')
        items = dict(container['items']); key = _managed_receipt_key(identity); envelope = items.get(key)
        if (type(envelope) is not dict or set(envelope) != {'payload','mac'} or type(envelope['payload']) is not dict
                or type(envelope['mac']) is not str or not hmac.compare_digest(envelope['mac'], _managed_receipt_mac(envelope['payload']))):
            raise ValueError('managed execution receipt authentication unavailable')
        receipt = dict(envelope['payload'])
        if (any(receipt.get(k) != identity[k] for k in ('sessionID','rootOperationID','actorID','executionRevision','innerTurnID'))
                or receipt.get('owner') != owner or receipt.get('accountID') != account_id
                or receipt.get('chatID') != chat_id or not _managed_receipt_binding(state, receipt)):
            raise ValueError('managed execution receipt changed')
        previous = receipt.get('executionFinish')
        if previous is not None:
            if previous != {'outcome':outcome}: raise ValueError('managed execution completion conflicts')
            return
        receipt['executionFinish'] = {'outcome':outcome}
        items[key] = {'payload':receipt,'mac':_managed_receipt_mac(receipt)}
        state['managed_turn_receipts'] = {'version':1,'items':items}; state['revision'] = int(state.get('revision') or 0) + 1
        row.mimo_state = state
        db.commit()


@dataclass(frozen=True)
class CanonicalSnapshot:
    session_id: str
    owner: str
    revision: int
    message_ids: tuple[str, ...]
    digest: str


def canonical_snapshot(session_id: str, owner: Optional[str] = None) -> CanonicalSnapshot:
    db = SessionLocal()
    try:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session is None or (owner is not None and (session.owner or "") != owner):
            raise KeyError(f"Canonical session {session_id!r} not found")
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.timestamp, ChatMessage.id)
            .all()
        )
        from src.openclank.session_operations import visibility
        excluded = visibility(session.mimo_state, require_settled=True)
        rows = [row for row in rows if row.id not in excluded]
        material = [
            {
                "id": row.id,
                "role": row.role,
                "content": row.content,
                "metadata": row.meta_data,
            }
            for row in rows
        ]
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return CanonicalSnapshot(
            session_id=session_id,
            owner=session.owner or "",
            revision=int(session.transcript_revision or 0),
            message_ids=tuple(row.id for row in rows),
            digest=digest,
        )
    finally:
        db.close()


def record_projection(
    snapshot: CanonicalSnapshot,
    *,
    mimo_session_id: str,
    workspace: str,
    endpoint_url: str,
    model: str,
    turn_id: str,
    mode_config_revision: int = 0,
) -> None:
    if not snapshot.owner:
        raise ValueError("Open Clank agent projections require an authenticated owner")
    db = SessionLocal()
    try:
        row = (
            db.query(MimoProjection)
            .filter(
                MimoProjection.odysseus_session_id == snapshot.session_id,
                MimoProjection.owner == snapshot.owner,
            )
            .first()
        )
        values = {
            "mimo_session_id": mimo_session_id,
            "owner": snapshot.owner,
            "workspace": workspace,
            "endpoint_url": endpoint_url,
            "model": model,
            "transcript_revision": snapshot.revision,
            "covered_message_ids": json.dumps(snapshot.message_ids),
            "canonical_digest": snapshot.digest,
            "mode_config_revision": mode_config_revision,
            "lifecycle_state": "active",
            "active_turn_id": turn_id,
        }
        if row is None:
            row = MimoProjection(odysseus_session_id=snapshot.session_id, **values)
            db.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def get_projection(session_id: str, owner: Optional[str] = None) -> Optional[dict]:
    if owner is None:
        raise ValueError("projection reads require an owner")
    db = SessionLocal()
    try:
        query = db.query(MimoProjection).filter(
            MimoProjection.odysseus_session_id == session_id
        )
        if owner is not None:
            query = query.filter(MimoProjection.owner == owner)
        try:
            row = query.first()
        except OperationalError:
            return None
        if row is None:
            return None
        return {
            "odysseus_session_id": row.odysseus_session_id,
            "mimo_session_id": row.mimo_session_id,
            "owner": row.owner,
            "workspace": row.workspace,
            "endpoint_url": row.endpoint_url,
            "model": row.model,
            "transcript_revision": row.transcript_revision,
            "covered_message_ids": json.loads(row.covered_message_ids or "[]"),
            "canonical_digest": row.canonical_digest,
            "mode_config_revision": row.mode_config_revision,
            "lifecycle_state": row.lifecycle_state,
            "active_turn_id": row.active_turn_id,
        }
    finally:
        db.close()


def list_projections(owner: Optional[str] = None) -> list[dict]:
    db = SessionLocal()
    try:
        query = db.query(MimoProjection)
        if owner is not None:
            query = query.filter(MimoProjection.owner == owner)
        try:
            rows = query.all()
        except OperationalError:
            return []
        return [
            {
                "odysseus_session_id": row.odysseus_session_id,
                "mimo_session_id": row.mimo_session_id,
                "owner": row.owner,
                "lifecycle_state": row.lifecycle_state,
            }
            for row in rows
        ]
    finally:
        db.close()


def mark_projection_stale(session_id: str, owner: Optional[str] = None) -> None:
    if owner is None:
        raise ValueError("projection updates require an owner")
    db = SessionLocal()
    try:
        query = db.query(MimoProjection).filter(MimoProjection.odysseus_session_id == session_id)
        if owner is not None:
            query = query.filter(MimoProjection.owner == owner)
        row = query.first()
        if row is not None:
            row.lifecycle_state = "stale"
            db.commit()
    finally:
        db.close()


def delete_projection(session_id: str, owner: Optional[str] = None) -> None:
    if owner is None:
        raise ValueError("projection deletes require an owner")
    db = SessionLocal()
    try:
        query = db.query(MimoProjection).filter(MimoProjection.odysseus_session_id == session_id)
        if owner is not None:
            query = query.filter(MimoProjection.owner == owner)
        query.delete(synchronize_session=False)
        db.commit()
    except OperationalError:
        db.rollback()
        raise RuntimeError("managed projection deletion was not confirmed")
    finally:
        db.close()


def get_mimo_state(session_id: str, owner: Optional[str] = None) -> dict:
    db = SessionLocal()
    try:
        query = db.query(Session).filter(Session.id == session_id)
        if owner is not None:
            query = query.filter(Session.owner == owner)
        session = query.first()
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        return dict(session.mimo_state or {})
    finally:
        db.close()


def assert_directory_execution_active(session_id: str, owner: str) -> None:
    """Refuse retired or malformed host execution scopes; history stays readable."""
    state = get_mimo_state(session_id, owner=owner)
    if "directory_retired_by" in state:
        raise RuntimeError("host directory execution scope is retired or invalid")


def retire_directory_execution_scope(session_id: str, owner: str, account: str,
                                     expected_binding: dict, digest: str, *, current_authority) -> str:
    """CAS the original host binding under the caller's retained account authority."""
    import re
    if (not owner or not account or not isinstance(expected_binding, dict)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not callable(current_authority)):
        raise ValueError("directory retirement authority unavailable")
    cwd = expected_binding.get("physicalCwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or os.path.realpath(cwd) != cwd:
        raise ValueError("directory retirement binding unavailable")
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = db.query(Session).filter(Session.id == session_id, Session.owner == owner).first()
        if row is None or current_authority(owner, account) is not True:
            raise ValueError("directory retirement account unavailable")
        state = dict(row.mimo_state or {})
        if state.get("managed_binding") != expected_binding or expected_binding.get("owner") != owner or expected_binding.get("stableChatID") != session_id:
            raise ValueError("directory retirement binding changed")
        old = state.get("directory_retired_by")
        if "directory_retired_by" in state:
            if old != digest or not isinstance(old, str) or not re.fullmatch(r"[0-9a-f]{64}", old):
                raise ValueError("directory retirement conflict")
            return old
        state["directory_retired_by"] = digest
        state["revision"] = int(state.get("revision") or 0) + 1
        row.mimo_state = state
        if current_authority(owner, account) is not True:
            raise ValueError("directory retirement account changed")
        db.commit()
        return digest
    finally:
        db.close()


def _directory_scope_snapshot(db, directory: str, owner: str, digest: str) -> tuple:
    """Bounded metadata only; unknown binding and foreign intersections refuse."""
    import copy
    import re
    from sqlalchemy import LargeBinary, case, cast, exists, func
    if (not owner or not isinstance(directory, str) or not os.path.isabs(directory)
            or os.path.realpath(directory) != directory or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("directory scope inventory unavailable")
    bounded_state = case(
        (func.length(cast(Session.mimo_state, LargeBinary)) <= 65536, Session.mimo_state),
        else_=None,
    )
    has_projection = exists().where(MimoProjection.odysseus_session_id == Session.id)
    rows = db.query(Session.id, Session.owner, bounded_state, Session.mimo_session_id, has_projection).order_by(Session.id).limit(513).all()
    if len(rows) > 512:
        raise ValueError("directory scope inventory exceeds its bound")
    result = []
    for chat_id, row_owner, raw_state, legacy_native_id, projected in rows:
        if not isinstance(chat_id, str) or not chat_id or len(chat_id) > 512 or not isinstance(row_owner, str) or not row_owner or len(row_owner) > 512 or not isinstance(raw_state, dict):
            raise ValueError("directory scope metadata unavailable")
        marker = raw_state.get("directory_retired_by")
        if "directory_retired_by" in raw_state:
            if not isinstance(marker, str) or not re.fullmatch(r"[0-9a-f]{64}", marker):
                raise ValueError("directory scope retirement invalid")
            if marker != digest:
                continue  # A previous operation already retired this scope.
        binding = raw_state.get("managed_binding")
        if binding is None and not raw_state and legacy_native_id is None and not projected:
            continue  # A fresh canonical chat has no native execution evidence.
        if not isinstance(binding, dict) or binding.get("owner") != row_owner or binding.get("stableChatID") != chat_id:
            raise ValueError("directory scope binding unavailable")
        cwd = binding.get("physicalCwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or os.path.realpath(cwd) != cwd:
            raise ValueError("directory scope cwd unavailable")
        if os.path.commonpath((directory, cwd)) != directory:
            continue
        if row_owner != owner:
            raise ValueError("foreign directory scope unavailable")
        result.append((chat_id, copy.deepcopy(binding), marker))
    return tuple(result)


def snapshot_directory_execution_scopes(directory: str, owner: str, account: str,
                                        digest: str, *, current_authority) -> tuple:
    """Private candidate tied to the caller's original operation, never a grant."""
    if not account or not callable(current_authority) or current_authority(owner, account, digest) is not True:
        raise ValueError("directory scope operation unavailable")
    db = SessionLocal()
    try:
        result = _directory_scope_snapshot(db, directory, owner, digest)
        if current_authority(owner, account, digest) is not True:
            raise ValueError("directory scope operation changed")
        return result
    finally:
        db.close()


def retire_directory_execution_scopes(directory: str, owner: str, account: str,
                                      digest: str, snapshot: tuple, *, current_authority) -> tuple:
    """CAS the complete original host set under the caller's held close."""
    if not account or not callable(current_authority) or not isinstance(snapshot, tuple):
        raise ValueError("directory scope authority unavailable")
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if current_authority(owner, account, digest) is not True:
            raise ValueError("directory scope account unavailable")
        actual = _directory_scope_snapshot(db, directory, owner, digest)
        if actual != snapshot:
            raise ValueError("directory scope inventory changed")
        for chat_id, binding, marker in actual:
            if marker == digest:
                continue
            row = db.query(Session).filter(Session.id == chat_id, Session.owner == owner).first()
            if row is None:
                raise ValueError("directory scope disappeared")
            state = dict(row.mimo_state or {})
            if state.get("managed_binding") != binding or "directory_retired_by" in state:
                raise ValueError("directory scope changed")
            state["directory_retired_by"] = digest
            state["revision"] = int(state.get("revision") or 0) + 1
            row.mimo_state = state
        if current_authority(owner, account, digest) is not True:
            raise ValueError("directory scope account changed")
        db.commit()
        return tuple(item[0] for item in actual)
    finally:
        db.close()


def mutate_plan_fields(session_id: str, owner: str, mutate, *, expected_binding=None, current_authority=None) -> dict:
    """Atomically own only plan and feedback fields; never wait on SQLite writers."""
    import copy
    if not owner or current_authority is None:
        raise ValueError("plan authority unavailable")
    db = SessionLocal()
    connection = None
    timeout = None
    begun = False
    try:
        connection = db.connection()
        if connection.dialect.name != "sqlite":
            raise RuntimeError("canonical plan transaction unavailable for this database")
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        connection.exec_driver_sql("PRAGMA busy_timeout=0")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        begun = True
        session = db.query(Session).filter(Session.id == session_id, Session.owner == owner).first()
        if session is None: raise KeyError("canonical session unavailable")
        state = dict(session.mimo_state or {})
        if not current_authority(): raise ValueError("plan account authority changed")
        if expected_binding is not None and state.get("managed_binding") != expected_binding:
            raise ValueError("plan managed binding changed")
        before = {key: copy.deepcopy(state.get(key)) for key in ("plan_state", "plan_feedback")}
        fields = copy.deepcopy(before)
        result = mutate(fields, copy.deepcopy(state.get("managed_binding")))
        if set(fields) != set(before): raise ValueError("invalid plan fields")
        if fields != before:
            for key, value in fields.items():
                if value is None: state.pop(key, None)
                else: state[key] = value
            state["revision"] = int(state.get("revision") or 0) + 1
            session.mimo_state = state
        if not current_authority(): raise ValueError("plan account authority changed")
        db.flush()
        connection.exec_driver_sql("COMMIT")
        begun = False
        return result
    except BaseException as exc:
        if connection is not None and begun:
            try: connection.exec_driver_sql("ROLLBACK")
            except Exception: connection.invalidate()
        if isinstance(exc, OperationalError):
            raise RuntimeError("canonical plan transaction unavailable") from exc
        raise
    finally:
        if connection is not None and timeout is not None:
            try: connection.exec_driver_sql("PRAGMA busy_timeout=" + str(int(timeout)))
            except Exception: connection.invalidate()
        try: db.rollback()
        finally: db.close()


def mutate_plan_state(session_id: str, owner: str, mutate, *, expected_binding=None, current_authority=None) -> dict:
    def apply(fields, _binding):
        old = fields["plan_state"]
        plan = mutate(dict(old) if isinstance(old, dict) else {})
        if not isinstance(plan, dict): raise ValueError("invalid canonical plan")
        fields["plan_state"] = plan
        return dict(plan)
    return mutate_plan_fields(session_id, owner, apply, expected_binding=expected_binding, current_authority=current_authority)


def save_mimo_state(
    session_id: str,
    state: dict,
    *,
    owner: Optional[str] = None,
    expected_workspace_revision: Optional[int] = None,
) -> dict:
    """Persist a secret-free negotiated control-plane snapshot."""
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        query = db.query(Session).filter(Session.id == session_id)
        if owner is not None:
            query = query.filter(Session.owner == owner)
        session = query.first()
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        previous = dict(session.mimo_state or {})
        if "directory_retired_by" in previous:
            import re
            marker = previous["directory_retired_by"]
            if not isinstance(marker, str) or not re.fullmatch(r"[0-9a-f]{64}", marker):
                raise ValueError("invalid directory retirement marker")
            if "directory_retired_by" in state and state["directory_retired_by"] != marker:
                raise ValueError("directory retirement marker cannot change")
        if expected_workspace_revision is not None:
            binding = previous.get("managed_binding")
            actual_workspace_revision = int(binding.get("workspaceRevision") or 0) if isinstance(binding, dict) else 0
            if actual_workspace_revision != int(expected_workspace_revision):
                raise ValueError("managed binding workspace revision conflict")
        before = {key: value for key, value in previous.items() if key != "revision"}
        after = {key: value for key, value in state.items() if key != "revision"}
        previous_binding = previous.get("managed_binding")
        # Whole-state callers never own the host binding. Preserve an existing
        # record unconditionally, and discard an attempted first write; host
        # binding creation/replacement goes through save_managed_binding so it
        # has one owner-qualified CAS boundary.
        if isinstance(previous_binding, dict):
            after["managed_binding"] = previous_binding
        else:
            after.pop("managed_binding", None)
        # Canonical plan and feedback have their own transaction owner.
        for key in ("plan_state", "plan_feedback", "selected_history_import", "directory_retired_by"):
            if key in previous: after[key] = previous[key]
            else: after.pop(key, None)
        # Native snapshot writers never own the recoverable history boundary.
        if "history_operation" in previous: after["history_operation"] = previous["history_operation"]
        else: after.pop("history_operation", None)
        # Native whole-state snapshots never own durable host turn receipts.
        if "managed_turn_receipts" in previous: after["managed_turn_receipts"] = previous["managed_turn_receipts"]
        else: after.pop("managed_turn_receipts", None)
        # Provider request options are owned by the route-selection helper.
        # ACP whole-state snapshots may be stale, so they cannot erase or
        # resurrect a newer route-bound option selection.
        if "model_request_options" in previous:
            after["model_request_options"] = previous["model_request_options"]
        else:
            after.pop("model_request_options", None)
        revision = int(previous.get("revision") or 0)
        if before != after:
            revision += 1
        # Whole-state writers are merge-safe: managed binding is owned by the
        # host cwd CAS and cannot be erased by a stale plan/model snapshot.
        payload = dict(after)
        payload["revision"] = revision
        session.mimo_state = payload
        db.commit()
        return payload
    finally:
        db.close()


def get_managed_binding(session_id: str, owner: Optional[str] = None) -> dict:
    """Return the detached owner-qualified host binding, if present."""
    if not owner:
        raise ValueError("managed binding reads require an owner")
    state = get_mimo_state(session_id, owner=owner)
    binding = state.get("managed_binding")
    return dict(binding) if isinstance(binding, dict) else {}


def save_managed_binding(
    session_id: str,
    binding: dict,
    *,
    owner: Optional[str] = None,
    expected_workspace_revision: Optional[int] = None,
    expected_engine_session_id: object = _EXPECTED_UNSET,
    expected_map_revision: object = _EXPECTED_UNSET,
    expected_mapping_revision: object = _EXPECTED_UNSET,
) -> dict:
    """Merge a host binding without clobbering unrelated session state."""
    if not owner:
        raise ValueError("managed binding writes require an owner")
    required_strings = (
        "owner", "stableChatID", "engineSessionID", "memoryWorkspaceID",
        "copalWorkspace", "physicalCwd",
    )
    if any(
        not isinstance(binding.get(key), str)
        or not binding[key]
        or binding[key] != binding[key].strip()
        for key in required_strings
    ):
        raise ValueError("managed binding is incomplete")
    # An ordinary chat may have no selected filesystem Workspace.  The
    # explicit empty string carries no Files authority; omission is invalid.
    authority_workspace = binding.get("authorityWorkspaceID")
    if not isinstance(authority_workspace, str) or authority_workspace != authority_workspace.strip():
        raise ValueError("managed binding authority workspace is invalid")
    if binding.get("owner") != owner or binding.get("stableChatID") != session_id or not os.path.isabs(binding["physicalCwd"]):
        raise ValueError("managed binding owner or cwd is invalid")
    if str(os.path.realpath(binding["physicalCwd"])) != binding["physicalCwd"]:
        raise ValueError("managed binding cwd is not canonical")
    aliases = binding.get("engineAliases")
    if (
        not isinstance(aliases, list)
        or len(aliases) > 16
        or any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            for value in aliases
        )
    ):
        raise ValueError("managed binding aliases are invalid")
    if len(set(aliases)) != len(aliases) or binding["engineSessionID"] in aliases:
        raise ValueError("managed binding aliases are not unique")
    if not isinstance(binding.get("memoryEnabled"), bool):
        raise ValueError("managed binding memory flag is invalid")
    for key in ("workspaceRevision", "mapRevision", "mappingRevision"):
        value = binding.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("managed binding revisions are invalid")
    transition = binding.get("transition")
    if transition is not None:
        if not isinstance(transition, dict) or transition.get("phase") not in {"in_flight", "reconciling"}:
            raise ValueError("managed binding transition is invalid")
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        query = db.query(Session).filter(Session.id == session_id)
        if owner is not None:
            query = query.filter(Session.owner == owner)
        session = query.first()
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        if not authority_workspace and str(session.workspace_id or "").strip():
            raise ValueError("managed binding cannot omit the selected workspace authority")
        state = dict(session.mimo_state or {})
        if "directory_retired_by" in state:
            raise RuntimeError("host directory execution scope is retired or invalid")
        previous = state.get("managed_binding")
        previous = dict(previous) if isinstance(previous, dict) else {}
        actual = int(previous.get("workspaceRevision") or 0)
        if expected_workspace_revision is not None and actual != int(expected_workspace_revision):
            raise ValueError("managed binding workspace revision conflict")
        actual_engine = str(previous.get("engineSessionID") or "") or None
        actual_map = int(previous.get("mapRevision") or 0)
        actual_mapping = int(previous.get("mappingRevision") or 0)
        if expected_engine_session_id is not _EXPECTED_UNSET and actual_engine != expected_engine_session_id:
            raise ValueError("managed binding engine-session conflict")
        if expected_map_revision is not _EXPECTED_UNSET and actual_map != int(expected_map_revision):
            raise ValueError("managed binding map revision conflict")
        if expected_mapping_revision is not _EXPECTED_UNSET and actual_mapping != int(expected_mapping_revision):
            raise ValueError("managed binding mapping revision conflict")
        candidate = dict(binding)
        candidate["workspaceRevision"] = int(candidate.get("workspaceRevision", actual))
        if previous and candidate.get("engineSessionID") != previous.get("engineSessionID"):
            same_axes = all(
                candidate.get(key) == previous.get(key)
                for key in ("workspaceRevision", "mapRevision", "mappingRevision")
            )
            if same_axes:
                raise ValueError("managed binding engine replacement lacks a mapping revision")
        state["managed_binding"] = candidate
        before = {key: value for key, value in state.items() if key != "revision"}
        old_state = dict(session.mimo_state or {})
        old_state.pop("revision", None)
        revision = int(session.mimo_state.get("revision", 0) if session.mimo_state else 0)
        if before != old_state:
            revision += 1
        state["revision"] = revision
        session.mimo_state = state
        db.commit()
        return dict(candidate)
    finally:
        db.close()


def delete_managed_binding(
    session_id: str,
    *,
    owner: Optional[str] = None,
    expected_workspace_revision: Optional[int] = None,
    expected_engine_session_id: object = _EXPECTED_UNSET,
    expected_map_revision: object = _EXPECTED_UNSET,
    expected_mapping_revision: object = _EXPECTED_UNSET,
) -> None:
    """Remove a host binding as compensation for an unexposed engine.

    This is deliberately owner-qualified and CAS-protected.  The engine
    admission transaction uses it only while the durable map still points at
    the candidate, so a competing winner cannot have its binding removed by a
    stale cleanup path.
    """
    if not owner:
        raise ValueError("managed binding deletes require an owner")
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        session = (
            db.query(Session)
            .filter(Session.id == session_id, Session.owner == owner)
            .first()
        )
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        state = dict(session.mimo_state or {})
        if "directory_retired_by" in state:
            raise RuntimeError("host directory execution scope is retired or invalid")
        previous = state.get("managed_binding")
        actual = int(previous.get("workspaceRevision") or 0) if isinstance(previous, dict) else 0
        if expected_workspace_revision is not None and actual != int(expected_workspace_revision):
            raise ValueError("managed binding workspace revision conflict")
        actual_engine = str(previous.get("engineSessionID") or "") or None if isinstance(previous, dict) else None
        actual_map = int(previous.get("mapRevision") or 0) if isinstance(previous, dict) else 0
        actual_mapping = int(previous.get("mappingRevision") or 0) if isinstance(previous, dict) else 0
        if expected_engine_session_id is not _EXPECTED_UNSET and actual_engine != expected_engine_session_id:
            raise ValueError("managed binding engine-session conflict")
        if expected_map_revision is not _EXPECTED_UNSET and actual_map != int(expected_map_revision):
            raise ValueError("managed binding map revision conflict")
        if expected_mapping_revision is not _EXPECTED_UNSET and actual_mapping != int(expected_mapping_revision):
            raise ValueError("managed binding mapping revision conflict")
        if not isinstance(previous, dict):
            db.commit()
            return
        state.pop("managed_binding", None)
        state["revision"] = int(state.get("revision") or 0) + 1
        session.mimo_state = state
        db.commit()
    finally:
        db.close()


async def purge_execution_projection(
    supervisor,
    session_id: str,
    *,
    owner: Optional[str] = None,
    _chat_guard=None,
) -> bool:
    """Securely remove any Open Clank agent execution state for a canonical session."""
    if owner is None:
        raise ValueError("projection purge requires an owner")
    row = get_projection(session_id, owner=owner)
    if row is not None and owner is not None and row["owner"] != owner:
        raise PermissionError("Open Clank agent projection belongs to another owner")
    effective_owner = owner or (row["owner"] if row is not None else None)

    if supervisor is not None and hasattr(supervisor, "mapped_sessions"):
        mapped = supervisor.mapped_sessions(owner=effective_owner)
    else:
        bridge = getattr(supervisor, "bridge", None) if supervisor else None
        mapped = bridge.mapped_sessions() if bridge is not None else {}
    mimo_session_id = (
        row["mimo_session_id"] if row is not None else mapped.get(session_id)
    )
    if mimo_session_id is None:
        return False
    try:
        alive = bool(supervisor and supervisor.is_alive(owner=effective_owner))
    except TypeError:
        alive = bool(supervisor and supervisor.is_alive())
    if not alive:
        raise RuntimeError("Open Clank agent is unavailable; execution state was not deleted")

    if _chat_guard is not None:
        await supervisor.delete_session(session_id, owner=effective_owner, mimo_session_id=mimo_session_id, _chat_guard=_chat_guard)
        return True
    try:
        await supervisor.delete_session(
            session_id,
            owner=effective_owner,
            mimo_session_id=mimo_session_id,
        )
    except TypeError:
        await supervisor.delete_session(
            session_id,
            mimo_session_id=mimo_session_id,
        )
    return True
