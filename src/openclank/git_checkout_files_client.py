"""Private, original-process custody for the checkout Files journal.

The caller owns Git acknowledgement, policy authorization and phase order. A
lost reply never authorizes another mutation; only original status is allowed.
"""
from __future__ import annotations

import json

from src.openclank.files_service_client import FilesServiceError, _checkpoint_owned_thread


class CheckoutFilesCustody:
    def __init__(self, client, process, path, history, operation_id, before_issue):
        self.client, self.process, self.path = client, process, path
        self.history, self.operation_id, self.before_issue = history, operation_id, before_issue
        self.issued = False
        self.uncertain = False
        self.inflight = False
        self.prepared = False

    @classmethod
    async def acquire(cls, client, path, operation_id, *, before_issue):
        if (client.transport != 'framed' or type(path) is not str or not path.startswith('/')
                or type(operation_id) is not str or not operation_id or not callable(before_issue)):
            raise FilesServiceError('checkout unavailable', code='checkout_unsent')
        def run():
            with client._lock:
                try:
                    client._assert_policy_generation()
                    client._refresh_history_binding_locked()
                    client._start_locked()
                    process = client._process
                    history = dict(client._history_environment_snapshot or {})
                    if process is None or process.poll() is not None or client._history_environment() != history:
                        raise ValueError('original process changed')
                    client._assert_policy_generation()
                    return cls(client, process, path, history, operation_id, before_issue)
                except Exception as error:
                    raise FilesServiceError('checkout unavailable', code='checkout_unsent') from error
        return await _checkpoint_owned_thread(run)

    async def exchange(self, operation, payload):
        allowed = {'checkout_prepare', 'checkout_step', 'checkout_status', 'checkout_rollback', 'checkout_finalize'}
        if operation not in allowed or type(payload) is not dict or payload.get('operation_id') != self.operation_id:
            raise FilesServiceError('checkout unavailable', code='checkout_unsent')
        try:
            frozen = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise FilesServiceError('checkout unavailable', code='checkout_unsent') from error
        is_status = operation == 'checkout_status'
        if self.inflight or (is_status and not self.issued) or (not is_status and self.uncertain):
            raise FilesServiceError('inspect original checkout status', code='checkout_unknown')
        if not is_status and ((operation == 'checkout_prepare') == self.prepared):
            raise FilesServiceError('checkout phase unavailable', code='checkout_unsent')
        self.inflight = True
        if not is_status:
            self.issued = True
            self.uncertain = True
        def run():
            with self.client._lock:
                try:
                    self.client._assert_policy_generation()
                    if (self.client._process is not self.process or self.process.poll() is not None
                            or self.client._history_environment() != self.history):
                        raise ValueError('original process changed')
                    if self.before_issue() is not True: raise ValueError('checkout authority changed')
                    request = self.client._request_envelope(operation, path=self.path,
                        payload=json.loads(frozen), root_operation_id=self.operation_id)
                    return self.client._exchange_locked(request)
                except Exception as error:
                    raise FilesServiceError('original checkout outcome unavailable', code='checkout_unknown') from error
        try:
            result = await _checkpoint_owned_thread(run)
            if not is_status:
                self.uncertain = False
                if operation == 'checkout_prepare': self.prepared = True
            return result
        finally:
            self.inflight = False

    async def probe_child(self, *, parent_path, root_id, parent_object_tag, component):
        """Read one named child, without enumerating unrelated directory entries."""
        if (self.inflight or self.uncertain or type(parent_path) is not str or not parent_path.startswith('/')
                or type(root_id) is not str or not root_id
                or (parent_object_tag is not None and (type(parent_object_tag) is not str
                    or len(parent_object_tag) != 64 or any(c not in '0123456789abcdef' for c in parent_object_tag)))
                or (component is not None and (type(component) is not str or not component or component in ('.','..')
                    or '/' in component or '\x00' in component or parent_object_tag is None))):
            raise FilesServiceError('checkout probe unavailable', code='checkout_unsent')
        self.inflight = True
        def run():
            with self.client._lock:
                try:
                    self.client._assert_policy_generation()
                    if (self.client._process is not self.process or self.process.poll() is not None
                            or self.client._history_environment() != self.history):
                        raise ValueError('original process changed')
                    if self.before_issue() is not True: raise ValueError('checkout authority changed')
                    request = self.client._request_envelope('checkout_probe_child', path=parent_path,
                        payload={'root_id':root_id,'parent_object_tag':parent_object_tag,'component':component},
                        root_operation_id=self.operation_id)
                    response = self.client._exchange_locked(request)
                    data = response.get('data')
                    if type(data) is not dict or data.get('state') not in ('present','absent','unknown'):
                        raise ValueError('invalid probe')
                    if data['state'] == 'present' and (set(data) != {'state','kind','object_tag'}
                            or data['kind'] not in ('file','directory','symlink','other')
                            or (data['object_tag'] is not None and (type(data['object_tag']) is not str
                                or len(data['object_tag']) != 64
                                or any(c not in '0123456789abcdef' for c in data['object_tag'])))):
                        raise ValueError('invalid probe')
                    if data['state'] != 'present' and set(data) != {'state'}: raise ValueError('invalid probe')
                    return data
                except Exception as error:
                    raise FilesServiceError('checkout probe unavailable', code='checkout_unknown') from error
        try:
            return await _checkpoint_owned_thread(run)
        finally:
            self.inflight = False
