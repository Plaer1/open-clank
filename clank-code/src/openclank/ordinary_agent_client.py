"""One Host-admitted ordinary writer using the existing rooted Agent Files owner."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping

from src.openclank.workflow_agent_client import RootedAgentFilesClient


class OrdinaryWriterClient(RootedAgentFilesClient):
    def __init__(self, owner, workspace, *, workspace_id, chat_id, history_root_id, history_root_path,
                 account_id, origin, canonical_tool, operation_id, native_digest, invocation_digest, target, current_sync):
        if (not callable(current_sync) or current_sync() is not True or canonical_tool not in {'write_file', 'edit_file'}
                or not isinstance(account_id, str) or not account_id or not isinstance(operation_id, str) or not operation_id
                or not re.fullmatch(r'[a-f0-9]{64}', native_digest or '')
                or not re.fullmatch(r'[a-f0-9]{64}', invocation_digest or '')
                or not isinstance(target, str) or not os.path.isabs(target) or os.path.realpath(target) != target
                or type(origin) is not dict):
            raise ValueError('ordinary writer unavailable')
        self._ordinary_current = current_sync
        self._ordinary_target = target
        self._ordinary_binding = {'operation_id': operation_id, 'request_digest': native_digest}
        self._ordinary_context = json.dumps({'version': 1, 'account_id': account_id, 'canonical_tool': canonical_tool,
            'operation_id': operation_id, 'request_digest': native_digest, 'invocation_digest': invocation_digest,
            'target_sha256': hashlib.sha256(target.encode('utf-8', 'strict')).hexdigest(),
            'managed_tool_origin_v1': origin}, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        super().__init__(owner, workspace, workspace_id=workspace_id, chat_id=chat_id,
            history_root_id=history_root_id, history_root_path=history_root_path)

    def _service_environment(self):
        if self._ordinary_current() is not True:
            raise ValueError('ordinary writer unavailable')
        env = super()._service_environment()
        context = json.loads(self._ordinary_context)
        if (self._ordinary_current() is not True or env.get('OPEN_CLANK_FILES_LANE') != 'agent'
                or env.get('OPENCLANK_HISTORY_ACCOUNT_ID') != context['account_id']):
            raise ValueError('ordinary writer unavailable')
        env['OPENCLANK_ORDINARY_WRITER_CONTEXT'] = self._ordinary_context
        return env

    def _check_operation(self, path, binding):
        if (self._ordinary_current() is not True or path != self._ordinary_target or os.path.realpath(path) != path
                or not isinstance(binding, Mapping) or any(binding.get(key) != value for key, value in self._ordinary_binding.items())):
            raise ValueError('ordinary writer unavailable')

    async def prepare_ordinary_replace(self, path, *, binding):
        self._check_operation(path, binding)
        return await super().prepare_ordinary_replace(path, binding=binding)

    async def ordinary_replace_status_exact(self, path, *, binding, capture_identity):
        self._check_operation(path, binding)
        return await super().ordinary_replace_status_exact(path, binding=binding, capture_identity=capture_identity)

    async def prepare_ordinary_create(self, path, *, binding):
        self._check_operation(path, binding)
        return await super().prepare_ordinary_create(path, binding=binding)

    async def ordinary_create_status_exact(self, path, *, binding, capture_identity):
        self._check_operation(path, binding)
        return await super().ordinary_create_status_exact(path, binding=binding, capture_identity=capture_identity)
