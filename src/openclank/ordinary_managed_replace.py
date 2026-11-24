"""Original Once custody for a Host-admitted ordinary managed replacement.

The caller owns scope, source and account authorization.  Preparing a receipt
is not evidence of a captured History action; apply is at most once.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from src.openclank.files_service_client import FilesServiceError


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8', 'strict')).hexdigest()


def validate_managed_origin(origin):
    """Validate the closed Host-verified call identity without inventing file state."""
    origin_fields = ('host_chat_id', 'engine_session_id', 'root_operation_id', 'actor_id',
                     'execution_revision', 'inner_turn_id', 'tool_call_id', 'assistant_message_id')
    if (set(origin) != {'version', *origin_fields} or origin.get('version') != 1
            or type(origin.get('version')) is not int or type(origin.get('execution_revision')) is not int
            or not 0 <= origin['execution_revision'] <= 9_007_199_254_740_991
            or any(not isinstance(origin.get(k), str) or not origin[k] or len(origin[k].encode('utf-8', 'strict')) > 256
                   or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in origin[k])
                   for k in origin_fields if k != 'execution_revision')):
        raise ValueError('ordinary managed origin unavailable')
    return [origin[k] for k in origin_fields]


def original_binding(*, account, workspace, origin, invocation_digest, path, expected, content):
    if (not isinstance(account, str) or not account or not isinstance(workspace, str) or not workspace
            or type(origin) is not dict or not re.fullmatch(r'[a-f0-9]{64}', invocation_digest or '')
            or not isinstance(path, str) or not os.path.isabs(path) or os.path.realpath(path) != path
            or not re.fullmatch(r'sha256:[0-9a-f]{64}:(0|[1-9][0-9]*)', expected or '')
            or type(content) is not bytes or len(content) > 262144 or b'\x00' in content):
        raise ValueError('ordinary managed replacement unavailable')
    content.decode('utf-8', 'strict')
    call = validate_managed_origin(origin)
    operation = 'model_' + _hash(['openclank.model-file.operation.v1', account, workspace, *call])
    action = 'model_' + _hash(['openclank.model-file.action.v1', operation])
    desired = hashlib.sha256(content).hexdigest()
    native_digest = _hash(['openclank.files.ordinary-replace.v1', account, path, expected, desired, operation, action])
    request_digest = _hash(['openclank.model-file.once.v1', account, workspace, *call,
                            invocation_digest, native_digest])
    binding = {'operation_id': operation, 'action_id': action, 'request_digest': native_digest,
               'timestamp_millis': int(time.time() * 1000), 'expected_fingerprint': expected,
               'desired_sha256': desired, 'desired_size': len(content)}
    receipt = {'version': 1, 'kind': 'ordinary.model.v1', 'state': 'pending', 'workspace': workspace,
               'target_digest': _hash(['openclank.model-file.target.v1', account, workspace, path]),
               'invocation_digest': invocation_digest, 'origin': dict(origin)}
    return operation, request_digest, binding, receipt


@dataclass
class PreparedManagedReplace:
    client: Any
    repository: Any
    account: str
    operation: str
    request_digest: str
    binding: dict
    claim: dict
    prepared: Any
    content: bytes
    current: Any
    used: bool = False

    def abort_unissued(self):
        if self.used:
            return False
        self.used = True
        try:
            self.repository.settle_collaboration_text(owner_subject_id=self.account, operation_id=self.operation,
                request_digest=self.request_digest, claim=self.claim, known_unsent=True)
            return True
        finally:
            self.client.close()

    async def apply(self):
        if self.used:
            return {'state': 'unknown'}
        self.used = True
        try:
            if await self.current() is not True:
                self.repository.settle_collaboration_text(owner_subject_id=self.account, operation_id=self.operation,
                    request_digest=self.request_digest, claim=self.claim, known_unsent=True)
                return {'state': 'unavailable'}
            self.repository.mark_collaboration_issued(owner_subject_id=self.account, operation_id=self.operation,
                request_digest=self.request_digest, claim=self.claim)
            await self.client.ordinary_replace(self.prepared, content=self.content)
            self.repository.retire_collaboration_io(owner_subject_id=self.account, operation_id=self.operation,
                request_digest=self.request_digest, claim=self.claim)
            response = await self.client.ordinary_replace_status(self.prepared)
            result = self.repository.settle_collaboration_text(owner_subject_id=self.account, operation_id=self.operation,
                request_digest=self.request_digest, claim=self.claim,
                native_status=response.get('data') if isinstance(response, dict) else None)
            if await self.current() is not True:
                return {'state': 'unknown'}
            return {'state': result.get('state', 'unknown'), 'revision': result.get('revision')}
        except BaseException as error:
            # Only the original owner's explicit pre-exchange refusal proves unsent.
            known_unsent = (self.prepared.used is False or
                (isinstance(error, FilesServiceError) and error.code == 'ordinary_write_unsent'
                 and self.prepared.settled is True))
            if known_unsent:
                try:
                    self.repository.settle_collaboration_text(owner_subject_id=self.account, operation_id=self.operation,
                        request_digest=self.request_digest, claim=self.claim, known_unsent=True)
                except Exception:
                    pass
            elif self.prepared.settled:
                try:
                    self.repository.retire_collaboration_io(owner_subject_id=self.account, operation_id=self.operation,
                        request_digest=self.request_digest, claim=self.claim)
                except Exception:
                    pass
            raise
        finally:
            self.client.close()


async def prepare(*, client, repository, account, workspace, generation, path, content, current,
                  operation, request_digest, binding, receipt):
    transferred = False
    reserved = False
    try:
        if await current() is not True:
            return None
        row = repository.reserve_operation(owner_subject_id=account, operation_id=operation,
            request_digest=request_digest, generation=generation, receipt=receipt, phase='ordinary.model.v1')
        if row is not None:
            # Reconciliation is status-only; the original producer alone may issue.
            return None
        reserved = True
        prepared = await client.prepare_ordinary_replace(path, binding=binding)
        capture = dict(prepared.capture_identity)
        if capture.get('account_id') != account or await current() is not True:
            raise ValueError('ordinary managed authority unavailable')
        claim = repository.claim_collaboration_text(owner_subject_id=account, operation_id=operation,
            request_digest=request_digest, generation=generation, workspace_id=workspace, path=path,
            binding=binding, capture_identity=capture)
        owner = PreparedManagedReplace(client, repository, account, operation, request_digest,
                                       dict(binding), claim, prepared, content, current)
        transferred = True
        return owner
    except BaseException:
        if reserved and not transferred:
            try:
                repository.refuse_collaboration_unclaimed(owner_subject_id=account, operation_id=operation,
                    request_digest=request_digest, state='unavailable')
            except Exception:
                pass
        raise
    finally:
        if not transferred:
            client.close()
