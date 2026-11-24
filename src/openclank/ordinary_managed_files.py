"""Current scoped Files reads for one admitted ordinary managed replacement.

This helper is not an authority: the caller holds the live Host account, turn,
policy and chat guard, and supplies the existing currentness witness.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping


async def read_existing(*, provider, context, repository, workspace, chat, requested_path,
                        action, args, current):
    from src.agent_tools.filesystem_tools import _agent_fingerprint, _agent_newline, _dominant_newline, _preserve_newline_style
    from src.openclank.workflow_agent_client import RootedAgentFilesClient
    from src.openclank.workflow_file_access import authorize_workflow_agent_root
    from src.openclank.files_service_client import FilesServiceError
    from src.openclank.filesystem_registry import FilesystemRootRegistry

    if not callable(current) or await current() is not True or action not in ('write', 'edit'):
        raise ValueError('ordinary file unavailable')
    path = os.path.expanduser(requested_path)
    if not os.path.isabs(path):
        path = os.path.join(workspace, path)
    path = os.path.abspath(os.path.normpath(path))
    if os.path.realpath(path) != path:
        raise ValueError('ordinary file unavailable')
    registry = FilesystemRootRegistry(repository=repository)
    scope = registry.agent_scope(context.owner_username, workspace, workspace_id=context.workspace_id, chat_id=chat)
    if scope.get('generation') != context.policy_generation:
        raise ValueError('ordinary file authority changed')
    broad_roots = scope.get('full_agent_roots') or {}
    choices = []
    for location in repository.list_locations():
        root = os.path.realpath(str(getattr(location, 'canonical_path', '') or ''))
        if getattr(location, 'kind', None) != 'directory' or not getattr(location, 'enabled', False) or not os.path.isabs(root):
            continue
        try:
            if os.path.commonpath((root, os.path.realpath(workspace))) != root or os.path.commonpath((root, path)) != root or path == root:
                continue
            candidate = await authorize_workflow_agent_root(provider, context, path=root, current=current,
                                                            workspace_root=workspace, chat_id=chat)
            choices.append(candidate)
        except Exception:
            continue
    choices.sort(key=lambda item: len(item['root']), reverse=True)
    if choices:
        if len(choices) > 1 and len(choices[0]['root']) == len(choices[1]['root']):
            raise ValueError('ordinary file unavailable')
        root = dict(choices[0])
    elif broad_roots:
        active = scope.get('active_folder') or {}
        root_id = active.get('root_id')
        if (len(broad_roots) != 1 or root_id not in broad_roots or active.get('canonical_path') != workspace
                or os.path.commonpath((workspace, path)) != workspace or path == workspace):
            raise ValueError('ordinary file unavailable')
        root = {'root_id':root_id,'root':broad_roots[root_id]['canonical_path'],'filesystem_access':'agent'}
    else:
        raise ValueError('ordinary file unavailable')
    client = RootedAgentFilesClient(context.owner_username, workspace, workspace_id=context.workspace_id,
        chat_id=chat, history_root_id=root['root_id'], history_root_path=root['root'])
    try:
        scope = client.app_scope
        if ('read' not in set((scope.get('root_capabilities') or {}).get(root['root_id']) or [])
                or 'write' not in set((scope.get('root_capabilities') or {}).get(root['root_id']) or [])):
            raise ValueError('ordinary file unavailable')
        if root.get('filesystem_access') == 'agent':
            env = client.history_binding_environment()
            if (env.get('OPENCLANK_HISTORY_ACCOUNT_ID') != context.owner_subject_id
                    or env.get('OPENCLANK_HISTORY_ACTOR_ID') != 'agent:' + context.owner_username
                    or not env.get('OPENCLANK_HISTORY_SOCKET') or not env.get('OPENCLANK_HISTORY_WORKSPACE_ID')
                    or await current() is not True):
                raise ValueError('ordinary file capture unavailable')
            root.update(history_actor=env['OPENCLANK_HISTORY_ACTOR_ID'],
                        history_workspace=env['OPENCLANK_HISTORY_WORKSPACE_ID'])
        try:
            response = await client.request('read_lines', path, {})
        except FilesServiceError as error:
            if action != 'write' or error.code != 'invalid_path' or args.get('expectedFingerprint') is not None:
                raise
            parent = await client.request('stat', os.path.dirname(path), {})
            pdata = parent.get('data') if isinstance(parent, Mapping) else None
            revision = pdata.get('directory_revision') if isinstance(pdata, Mapping) else None
            desired = args['content'].encode('utf-8', 'strict')
            if (not isinstance(revision,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',revision)
                    or len(desired)>262144 or b'\x00' in desired or await current() is not True):
                raise ValueError('ordinary create unavailable')
            return {'kind':'create','path':path,'root':root,'expected':'missing','parent_revision':revision,
                    'content':desired,'newline':{'\n':'lf','\r\n':'crlf','\r':'cr'}[_dominant_newline(args['content'])],
                    'lines':len(args['content'].splitlines())}
        data = response.get('data') if isinstance(response, Mapping) else None
        if not isinstance(data, Mapping) or not isinstance(data.get('text'), str):
            raise ValueError('ordinary file unavailable')
        text = data['text']; raw = text.encode('utf-8', 'strict'); digest = hashlib.sha256(raw).hexdigest()
        fingerprint = f'sha256:{digest}:{len(raw)}'
        # Refuse encodings or representations whose exact original bytes were not read.
        if (len(raw) > 262144 or type(data.get('size')) is not int or data['size'] != len(raw)
                or _agent_fingerprint(data) != fingerprint or b'\x00' in raw or await current() is not True):
            raise ValueError('ordinary file unavailable')
        if args.get('expectedFingerprint') is not None and args['expectedFingerprint'] != fingerprint:
            raise ValueError('ordinary file changed')
        if action == 'write':
            newline = {'lf':'\n', 'crlf':'\r\n', 'cr':'\r'}.get(_agent_newline(data.get('newline')), '\n')
            rendered = _preserve_newline_style(args['content'], newline)
        else:
            old, new = args['oldText'], args['newText']; count = text.count(old)
            if not count or (count != 1 and not args['replaceAll']):
                raise ValueError('ordinary edit unavailable')
            rendered = text.replace(old, new) if args['replaceAll'] else text.replace(old, new, 1)
        desired = rendered.encode('utf-8', 'strict')
        if len(desired) > 262144 or b'\x00' in desired or await current() is not True:
            raise ValueError('ordinary file unavailable')
        return {'kind':'replace', 'path':path, 'root':root, 'expected':fingerprint, 'content':desired,
                'newline':{'\n':'lf','\r\n':'crlf','\r':'cr'}[_dominant_newline(rendered)], 'lines':len(rendered.splitlines())}
    finally:
        client.close()


async def qualified_locator(*, provider, context, workspace, chat, root, path, action_id, origin,
                            expected, fingerprint, operation, tool, history_supervisor, current):
    """Read the real action, then ask the authorized History owner for its locator."""
    from src.openclank.files_host_provider import _origin
    from src.openclank.files_service_client import _checkpoint_owned_thread
    from src.openclank.history_change_sets import parse_locator
    from src.openclank.history_client import HistoryClient, registered_resource_pages
    from src.openclank.workflow_agent_client import RootedAgentFilesClient
    from src.openclank.workflow_file_access import lookup_workflow_history_identity

    if await current() is not True:
        raise ValueError('ordinary capture unavailable')
    client = RootedAgentFilesClient(context.owner_username, workspace, workspace_id=context.workspace_id,
        chat_id=chat, history_root_id=root['root_id'], history_root_path=root['root'])
    try:
        env = client.history_binding_environment()
        if (env.get('OPENCLANK_HISTORY_ACCOUNT_ID') != context.owner_subject_id
                or env.get('OPENCLANK_HISTORY_ACTOR_ID') != root['history_actor']
                or env.get('OPENCLANK_HISTORY_WORKSPACE_ID') != root['history_workspace']
                or not env.get('OPENCLANK_HISTORY_TOKEN') or await current() is not True):
            raise ValueError('ordinary capture unavailable')
        capture = HistoryClient(env['OPENCLANK_HISTORY_SOCKET'], actor_id=root['history_actor'],
            account_id=context.owner_subject_id, token=env['OPENCLANK_HISTORY_TOKEN'], timeout=1.5)
        response = await _checkpoint_owned_thread(lambda: capture.get_capture_action(action_id))
        action = response.get('Action') if isinstance(response, Mapping) else None
        before = action.get('before_resources') if isinstance(action, Mapping) else None
        after = action.get('after_resources') if isinstance(action, Mapping) else None
        live = action.get('live') if isinstance(action, Mapping) else None
        if (not isinstance(action, Mapping) or action.get('action_id') != action_id
                or action.get('actor_id') != root['history_actor'] or action.get('actor_account_id') != context.owner_subject_id
                or action.get('state') != 'Complete' or action.get('managed_tool_origin_v1') != origin
                or not isinstance(before, list) or len(before) != 1 or not isinstance(after, list) or len(after) != 1
                or not isinstance(before[0], Mapping) or not isinstance(after[0], Mapping)):
            raise ValueError('ordinary capture unavailable')
        before_receipt=before[0].get('before')
        if (operation not in ('create','replace') or action.get('operation') != operation or action.get('tool_id') != tool
                or (operation == 'create' and (tool != 'write_file' or expected != 'missing'
                    or before[0].get('existence') != 'Absent' or not isinstance(before_receipt,Mapping)
                    or before_receipt.get('content') != 'Tombstone' or before_receipt.get('fingerprint') != 'missing'))
                or (operation == 'replace' and before[0].get('existence') != 'Present')
                or after[0].get('existence') != 'Present'):
            raise ValueError('ordinary capture unavailable')
        key = after[0].get('resource_key')
        if (not isinstance(key, Mapping) or key != before[0].get('resource_key')
                or key.get('account_id') != context.owner_subject_id or key.get('workspace_id') != root['history_workspace']
                or key.get('provider') != 'odysseus-files' or not isinstance(key.get('resource_id'), str)
                or before[0].get('fingerprint') != expected or after[0].get('fingerprint') != fingerprint
                or not isinstance(live, Mapping) or live.get('status') != 'Committed' or live.get('fingerprint') != fingerprint
                or await current() is not True):
            raise ValueError('ordinary capture unavailable')
        if root.get('filesystem_access') == 'agent':
            # The capture belongs to the exact Agent actor. A Human Off
            # account can receive the committed action without browsing it.
            relative = os.path.relpath(path, root['root'])
            resource = key['resource_id']
            matches = 0
            process = history_supervisor.process

            async def fetch(cursor):
                return await _checkpoint_owned_thread(lambda: capture.list_registered_resources(cursor=cursor, limit=100))

            async for page in registered_resource_pages(fetch):
                if await current() is not True or history_supervisor.process is not process:
                    raise ValueError('ordinary capture unavailable')
                matches += sum(
                    row.get('resource_id') == resource and row.get('active') is True
                    and row.get('account_id') == context.owner_subject_id and row.get('workspace_id') == root['history_workspace']
                    and row.get('root_id') == root['root_id'] and row.get('root_path') == root['root']
                    and row.get('actor_ids') == [root['history_actor']] and row.get('relative_path') == relative
                    for row in page['items']
                )
            if matches != 1 or await current() is not True or history_supervisor.process is not process:
                raise ValueError('ordinary capture unavailable')
            reader = capture
        else:
            resource = await lookup_workflow_history_identity(provider, context, root_origin_id=_origin(root['root']),
                root_revision=root['location_revision'], path=path, history_supervisor=history_supervisor,
                current=current, workspace_root=workspace, chat_id=chat)
            if resource != key['resource_id'] or await current() is not True:
                raise ValueError('ordinary capture unavailable')
            reader = history_supervisor.client_for(context.owner_username, context.owner_subject_id)
        locator = await _checkpoint_owned_thread(lambda: reader.get_managed_action_locator(
            resource_id=resource, action_id=action_id, origin=origin))
        parsed = parse_locator(locator)
        if (parsed.get('actionID') != action_id or parsed.get('resourceID') != resource
                or parsed.get('origin') != origin or await current() is not True):
            raise ValueError('ordinary capture unavailable')
        await _checkpoint_owned_thread(lambda: reader.get_managed_resource_change(resource_id=resource, action_id=action_id,
            before_version_id=parsed['beforeVersionID'], after_version_id=parsed['afterVersionID'], origin=origin))
        if await current() is not True:
            raise ValueError('ordinary capture unavailable')
        return locator
    finally:
        client.close()
