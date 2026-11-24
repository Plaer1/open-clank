"""Private, account-scoped History capture for actual Imps project state.

Callers own project authorization and CAS. History receipts are evidence, not
permission to read or mutate a project.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any


class ProjectHistoryUnavailable(RuntimeError):
    """The exact History operation cannot safely be issued or read."""


def _identity(value: str) -> str:
    if type(value) is not str or not value or len(value.encode('utf-8', 'strict')) > 256 or any(ord(c) < 32 for c in value):
        raise ProjectHistoryUnavailable('project history identity unavailable')
    return value


def _fingerprint(data: bytes | None) -> str:
    return 'missing' if data is None else f'sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}'


def _intent_metadata(coverage: Any) -> dict[str, Any] | None:
    metadata = coverage.get('metadata') if isinstance(coverage, dict) else None
    if not isinstance(metadata, dict):
        return None
    values = dict(metadata)
    if values.pop('required_recovery', True) is not True:
        return None
    return values


def _revision(value: int | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise ProjectHistoryUnavailable('project history revision unavailable')
    return {'Opaque': {'kind': 'imps-project-revision-v1', 'value': str(value)}}


@dataclass(frozen=True)
class PreparedProjectCapture:
    action_id: str
    project_id: str
    resource: dict[str, str]
    before_fingerprint: str


class ImageProjectHistory:
    def __init__(self, client: Any, *, account_id: str, actor_id: str):
        self.account_id, self.actor_id = _identity(account_id), _identity(actor_id)
        if getattr(client, 'account_id', None) != self.account_id or getattr(client, 'actor_id', None) != self.actor_id:
            raise ProjectHistoryUnavailable('project history credential unavailable')
        self._client = client

    def _key(self, project_id: str) -> dict[str, str]:
        return {'account_id': self.account_id, 'workspace_id': 'imps-projects', 'provider': 'imps-project', 'resource_id': _identity(project_id)}

    def _action(self, action_id: str, project_id: str) -> dict[str, Any]:
        result = self._client.get_capture_action(_identity(action_id))
        action = result.get('Action') if isinstance(result, dict) else None
        key = self._key(project_id)
        if (type(action) is not dict or action.get('action_id') != action_id or action.get('resource_key') != key
                or action.get('actor_account_id') != self.account_id or action.get('actor_id') != self.actor_id or action.get('actor_kind') != 'host'
                or action.get('tool_id') != 'imps-project' or not str(action.get('operation', '')).startswith('imps-project:')
                or action.get('guard_resource_ids') != [key] or action.get('modified_resource_ids') != [key]):
            raise ProjectHistoryUnavailable('project history action unavailable')
        return action

    def _before(self, action: dict[str, Any], key: dict[str, str]) -> bytes | None:
        members = action.get('before_resources')
        if type(members) is not list or len(members) != 1 or type(members[0]) is not dict:
            raise ProjectHistoryUnavailable('project history before unavailable')
        member = members[0]
        receipt = member.get('before')
        coverage = action.get('coverage')
        binding = _intent_metadata(coverage)
        marker = binding.get('batch_once') if type(binding) is dict else None
        if (type(binding) is not dict or set(binding) != {'imps_intent_sha256', 'batch_once'}
                or type(marker) is not dict or set(marker) != {'version', 'binding_sha256'}
                or type(marker.get('version')) is not int or marker['version'] != 1 or type(binding.get('imps_intent_sha256')) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', binding['imps_intent_sha256'])
                or marker.get('binding_sha256') != binding['imps_intent_sha256']
                or type(member.get('metadata')) is not dict or member['metadata'].get('opaque') != marker):
            raise ProjectHistoryUnavailable('project history binding unavailable')
        if member.get('resource_key') != key or member.get('resource_type') != 'Other' or member.get('existence') not in ('Present', 'Absent') or type(receipt) is not dict:
            raise ProjectHistoryUnavailable('project history before unavailable')
        data = self._client.read_version(action['action_id'], receipt)
        if (data is not None and type(data) is not bytes) or (member['existence'] == 'Absent') != (data is None) or member.get('fingerprint') != _fingerprint(data) or receipt.get('fingerprint') != _fingerprint(data):
            raise ProjectHistoryUnavailable('project history before mismatch')
        return data

    def _after(self, action: dict[str, Any], key: dict[str, str]) -> bytes | None:
        # The explicit single-resource completion records the primary receipt,
        # never a partial projection of a multi-resource batch.
        self._before(action, key)
        live = action.get('live')
        if (action.get('state') != 'Complete' or action.get('after_resources') != []
                or type(live) is not dict or live.get('action_id') != action['action_id']
                or live.get('status') != 'Committed' or type(action.get('after')) is not dict):
            raise ProjectHistoryUnavailable('project history after unavailable')
        receipt = action['after']
        data = self._client.read_version(action['action_id'], receipt)
        if ((data is not None and type(data) is not bytes) or receipt.get('fingerprint') != _fingerprint(data)
                or live.get('fingerprint') != _fingerprint(data)):
            raise ProjectHistoryUnavailable('project history after mismatch')
        return data

    def prepare(self, *, action_id: str, project_id: str, expected_revision: int | None, before: bytes | None, operation: str, intent_sha256: str) -> PreparedProjectCapture:
        _identity(action_id)
        if type(operation) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', operation) or type(intent_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', intent_sha256) or (before is not None and type(before) is not bytes):
            raise ProjectHistoryUnavailable('project history request unavailable')
        key, revision = self._key(project_id), _revision(expected_revision)
        if (before is None) != (expected_revision is None):
            raise ProjectHistoryUnavailable('project history preimage unavailable')
        fp = _fingerprint(before)
        marker = {'version': 1, 'binding_sha256': intent_sha256}
        request = {'action_id': action_id, 'actor_account_id': self.account_id, 'resource_key': key,
                   'guard_resource_ids': [key], 'modified_resource_ids': [key], 'operation': 'imps-project:' + operation,
                   'expected_revision': revision, 'actor_id': self.actor_id, 'actor_kind': 'host', 'tool_id': 'imps-project', 'before_revision': revision,
                   'coverage': {'content_digest': None, 'metadata_digest': None, 'byte_len': None, 'metadata': {'imps_intent_sha256': intent_sha256, 'batch_once': marker, 'required_recovery': True}}}
        entry = {'resource_key': key, 'old_locator': None, 'new_locator': None, 'expected_revision': revision,
                 'existence': 'Absent' if before is None else 'Present', 'resource_type': 'Other',
                 'metadata': {'mode': None, 'size': None if before is None else len(before), 'modified_millis': None, 'opaque': marker},
                 'content': before, 'fingerprint': fp,
                 'coverage': {'content_digest': None, 'metadata_digest': None, 'byte_len': None if before is None else len(before), 'metadata': {'exact_preimage': True}}}
        try:
            result = self._client.prepare_batch_once(request, [entry])
            if result.get('created') is not True:
                raise ProjectHistoryUnavailable('original project operation already exists; status only')
            action = self._action(action_id, project_id)
            if (action.get('state') != 'BeforeDurable' or action.get('operation') != request['operation']
                    or action.get('expected_revision') != revision or action.get('before_revision') != revision
                    or action.get('coverage') != request['coverage']
                    or self._before(action, key) != before):
                raise ProjectHistoryUnavailable('project history preparation mismatch')
            return PreparedProjectCapture(action_id, project_id, key, fp)
        except ProjectHistoryUnavailable:
            raise
        except Exception as exc:
            raise ProjectHistoryUnavailable('project history preparation unavailable') from exc

    def read_before(self, *, action_id: str, project_id: str) -> bytes | None:
        try:
            action = self._action(action_id, project_id)
            if action.get('state') not in ('BeforeDurable', 'Applying', 'Applied', 'CapturingAfter', 'AfterDurable', 'Complete', 'Failed', 'Conflict', 'Partial', 'Aborted', 'AfterCaptureFailed', 'NeedsReconciliation'):
                raise ProjectHistoryUnavailable('project history before unavailable')
            return self._before(action, self._key(project_id))
        except ProjectHistoryUnavailable:
            raise
        except Exception as exc:
            raise ProjectHistoryUnavailable('project history read unavailable') from exc

    def original(self, *, action_id: str) -> dict[str, str] | None:
        """Select only this actor's original action; the caller must reauthorize its project."""
        try:
            response = self._client.get_capture_action(_identity(action_id))
            action = response.get('Action') if isinstance(response, dict) else None
            key = action.get('resource_key') if type(action) is dict else None
            if type(key) is not dict or set(key) != {'account_id', 'workspace_id', 'provider', 'resource_id'}:
                return None
            project_id = _identity(key.get('resource_id'))
            action = self._action(action_id, project_id)
            coverage = action.get('coverage')
            metadata = _intent_metadata(coverage)
            if type(metadata) is not dict or set(metadata) != {'imps_intent_sha256', 'batch_once'}:
                return None
            intent = metadata['imps_intent_sha256']
            if type(intent) is not str or not re.fullmatch(r'[0-9a-f]{64}', intent):
                return None
            return {'project_id': project_id, **self.status(action_id=action_id, project_id=project_id, intent_sha256=intent)}
        except Exception:
            return None

    def status(self, *, action_id: str, project_id: str, intent_sha256: str) -> dict[str, str]:
        """Observe an exact original operation; absence never permits a new issue."""
        result = {'action_id': _identity(action_id), 'outcome': 'unknown', 'history_status': 'unknown'}
        if type(intent_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', intent_sha256):
            raise ProjectHistoryUnavailable('project history intent unavailable')
        try:
            action = self._action(action_id, project_id)
            coverage = action.get('coverage')
            if _intent_metadata(coverage) != {'imps_intent_sha256': intent_sha256, 'batch_once': {'version': 1, 'binding_sha256': intent_sha256}}:
                return result
            settled = action.get('recovery_only') is True and action.get('recovery_settled') is True
            if not settled:
                self._before(action, self._key(project_id))
            live = action.get('live')
            if type(live) is not dict or live.get('action_id') != action_id:
                return result
            outcome = {'Committed':'committed', 'NotCommitted':'not_committed', 'Conflict':'conflict', 'Partial':'partial', 'Unknown':'unknown'}.get(live.get('status'))
            if outcome is None:
                return result
            result['outcome'] = outcome
            result['history_status'] = 'unavailable' if outcome == 'committed' else outcome
            if outcome == 'committed' and action.get('state') == 'Complete':
                if settled:
                    result['history_status'] = 'skipped'
                else:
                    self._after(action, self._key(project_id))
                    result['history_status'] = self._finish_verified(action)
        except Exception:
            pass
        return result

    def _settle_committed_gap(self, prepared: PreparedProjectCapture, after: bytes | None) -> bool:
        """Observe a completed after-state or record its known capture gap, never reissue."""
        try:
            action = self._action(prepared.action_id, prepared.project_id)
            if (_fingerprint(self._before(action, prepared.resource)) != prepared.before_fingerprint
                    or action.get('after_resources') != []):
                return False
            live = action.get('live')
            if (type(live) is not dict or live.get('action_id') != prepared.action_id
                    or live.get('status') != 'Committed' or live.get('fingerprint') != _fingerprint(after)):
                return False
            if action.get('state') == 'Complete':
                return self._after(action, prepared.resource) == after
            if action.get('state') in ('Applied', 'CapturingAfter'):
                self._client.after_unavailable(prepared.action_id)
            return False
        except Exception:
            return False

    def _finish_verified(self, action: dict[str, Any]) -> str:
        if action.get('recovery_only') is True:
            self._client.settle_recovery(action['action_id'])
            return 'skipped'
        return 'complete'

    def finish(self, prepared: PreparedProjectCapture, *, outcome: str, after: bytes | None) -> dict[str, str]:
        if type(prepared) is not PreparedProjectCapture or prepared.resource != self._key(prepared.project_id) or outcome not in ('committed', 'not_committed', 'conflict', 'partial', 'unknown') or (after is not None and type(after) is not bytes):
            raise ProjectHistoryUnavailable('project history completion unavailable')
        status = {'committed': 'Committed', 'not_committed': 'NotCommitted', 'conflict': 'Conflict', 'partial': 'Partial', 'unknown': 'Unknown'}[outcome]
        result = {'action_id': prepared.action_id, 'outcome': outcome, 'history_status': 'unknown' if outcome == 'unknown' else 'unavailable'}
        try:
            action = self._action(prepared.action_id, prepared.project_id)
            if action.get('state') != 'BeforeDurable' or _fingerprint(self._before(action, prepared.resource)) != prepared.before_fingerprint:
                return result
            self._client.record_live(prepared.action_id, {'action_id': prepared.action_id, 'status': status, 'fingerprint': _fingerprint(after) if outcome == 'committed' else None, 'committed_resources': []})
            if outcome != 'committed':
                if outcome in ('not_committed', 'conflict') and action.get('recovery_only') is True:
                    self._client.settle_recovery(prepared.action_id)
                result['history_status'] = 'not_committed' if outcome == 'not_committed' else outcome
                return result
            action = self._action(prepared.action_id, prepared.project_id)
            live = action.get('live')
            if (action.get('state') != 'Applied' or action.get('after_resources') != [] or type(live) is not dict
                    or live.get('action_id') != prepared.action_id or live.get('status') != 'Committed'
                    or live.get('fingerprint') != _fingerprint(after)):
                return result
            self._before(action, prepared.resource)
            self._client.complete(prepared.action_id, content=after, fingerprint=_fingerprint(after))
            action = self._action(prepared.action_id, prepared.project_id)
            if self._after(action, prepared.resource) == after:
                result['history_status'] = self._finish_verified(action)
        except Exception:
            if outcome == 'committed' and self._settle_committed_gap(prepared, after):
                result['history_status'] = self._finish_verified(self._action(prepared.action_id, prepared.project_id))
        return result
