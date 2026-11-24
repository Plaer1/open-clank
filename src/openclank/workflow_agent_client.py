"""Fresh Agent-lane Files client with Host-derived delegated writer provenance.

This metadata is attribution, not an authorization credential. The ordinary
AgentScope and scoped History service principal remain the authority owners.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping

from src.openclank.files_service_client import AgentFilesClient
from src.openclank.filesystem_registry import FilesystemRootRegistry

_FIELDS=('session_id','actor_id','execution_revision','root_operation_id','inner_turn_id')


def _turn(value):
    if type(value) is not dict or set(value) != set(_FIELDS): raise ValueError('workflow writer unavailable')
    out=dict(value)
    for key in _FIELDS:
        item=out[key]
        if key=='execution_revision':
            if type(item) is not int or not 0<=item<=9007199254740991: raise ValueError('workflow writer unavailable')
        elif not isinstance(item,str) or not item or item != item.strip() or len(item.encode('utf-8'))>256 or any(ord(ch)<32 for ch in item):
            raise ValueError('workflow writer unavailable')
    return out


class RootedAgentFilesClient(AgentFilesClient):
    """Agent Files scope with an independently selected existing History root."""
    def __init__(self, owner, workspace, *, workspace_id, chat_id, history_root_id, history_root_path):
        if (not isinstance(history_root_id,str) or not history_root_id or not isinstance(history_root_path,str)
                or not os.path.isabs(history_root_path) or os.path.realpath(history_root_path)!=history_root_path):
            raise ValueError('workflow History root unavailable')
        self._workflow_history_root_id=history_root_id
        self._workflow_history_root_path=history_root_path
        super().__init__(owner,workspace,workspace_id=workspace_id,chat_id=chat_id,
            registered_only=not history_root_id.startswith(FilesystemRootRegistry.FULL_AGENT_ROOT_PREFIX))
        # Files refreshes History before first process start; prime the actual
        # AgentScope so that refresh can select the authorized History root.
        self._service_environment()

    def _history_environment(self):
        previous=self.app_scope
        root=self._workflow_history_root_id
        if root not in set(previous.get('visible_root_ids') or []):
            raise ValueError('workflow History root unavailable')
        narrowed=dict(previous); narrowed['visible_root_ids']=[root]; narrowed['selected_history_root_id']=root
        narrowed['root_capabilities']={root:(previous.get('root_capabilities') or {}).get(root,[])}
        self.app_scope=narrowed
        try: env=super()._history_environment()
        finally: self.app_scope=previous
        bindings=json.loads(env.get('OPENCLANK_HISTORY_ROOT_BINDINGS','[]'))
        if (len(bindings)!=1 or bindings[0].get('root_id')!=root
                or bindings[0].get('canonical_path')!=self._workflow_history_root_path
                or env.get('OPENCLANK_HISTORY_ACTOR_ID')!='agent:'+self.owner
                or not env.get('OPENCLANK_HISTORY_ACCOUNT_ID') or not env.get('OPENCLANK_HISTORY_WORKSPACE_ID')):
            raise ValueError('workflow History root unavailable')
        return env

    def history_binding_environment(self):
        # Agent scope is freshly resolved by the existing client owner.
        return self._service_environment()


class WorkflowAgentFilesClient(RootedAgentFilesClient):
    """One private client for one already-admitted strict Files operation."""
    def __init__(self, owner: str, workspace: str, *, workspace_id: str, chat_id: str,
                 account_id: str, writer: dict, source: dict, tool_call_id: str,
                 operation_id: str, request_digest: str, target: str, current_sync, history_root_id: str, history_root_path: str):
        writer=_turn(writer); source=_turn(source)
        if (not callable(current_sync) or current_sync() is not True or not isinstance(account_id,str) or not account_id
                or any(not isinstance(v,str) or not v or v != v.strip() or len(v.encode('utf-8'))>256 or any(ord(ch)<32 for ch in v)
                       for v in (owner,workspace_id,chat_id,tool_call_id,operation_id))
                or not isinstance(request_digest,str) or not re.fullmatch(r'[a-f0-9]{64}',request_digest)
                or not isinstance(target,str) or not os.path.isabs(target) or os.path.realpath(target)!=target
                or writer['root_operation_id']!=source['root_operation_id']):
            raise ValueError('workflow writer unavailable')
        body={'version':1,'account_id':account_id,**writer,'tool_call_id':tool_call_id,'source':source,
              'operation_id':operation_id,'request_digest':request_digest,
              'target_sha256':hashlib.sha256(target.encode('utf-8','strict')).hexdigest()}
        self._workflow_context=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        self._workflow_current=current_sync
        super().__init__(owner,workspace,workspace_id=workspace_id,chat_id=chat_id,history_root_id=history_root_id,history_root_path=history_root_path)

    def _service_environment(self):
        if self._workflow_current() is not True: raise ValueError('workflow writer unavailable')
        env=super()._service_environment()
        if (self._workflow_current() is not True or env.get('OPEN_CLANK_FILES_LANE')!='agent'
                or env.get('OPENCLANK_HISTORY_ACCOUNT_ID') != json.loads(self._workflow_context)['account_id']):
            raise ValueError('workflow writer unavailable')
        env['OPENCLANK_WORKFLOW_WRITER_CONTEXT']=self._workflow_context
        return env

    def _check_operation(self, path, binding):
        try:
            context=json.loads(self._workflow_context)
            valid=(self._workflow_current() is True and isinstance(path,str) and os.path.isabs(path) and os.path.realpath(path)==path
                and hashlib.sha256(path.encode('utf-8','strict')).hexdigest()==context['target_sha256']
                and isinstance(binding,Mapping) and binding.get('operation_id')==context['operation_id']
                and binding.get('request_digest')==context['request_digest'])
        except Exception:
            valid=False
        if not valid: raise ValueError('workflow writer unavailable')

    async def prepare_ordinary_replace(self,path,*,binding):
        self._check_operation(path,binding)
        return await super().prepare_ordinary_replace(path,binding=binding)

    async def prepare_ordinary_create(self,path,*,binding):
        self._check_operation(path,binding)
        return await super().prepare_ordinary_create(path,binding=binding)

    async def ordinary_replace_status_exact(self,path,*,binding,capture_identity):
        self._check_operation(path,binding)
        return await super().ordinary_replace_status_exact(path,binding=binding,capture_identity=capture_identity)

    async def ordinary_create_status_exact(self,path,*,binding,capture_identity):
        self._check_operation(path,binding)
        return await super().ordinary_create_status_exact(path,binding=binding,capture_identity=capture_identity)
