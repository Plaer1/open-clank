"""User-directed preview, promotion, file-copy exchange and workspace publication."""
import json
import sqlite3
from pathlib import Path
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response
from routes.general_hex_routes import _owner, _human_change
from src.general_hex_library import GeneralHexError, GeneralHexConflict, GeneralHexNotFound
from src.general_hex_composition import GeneralHexComposition
from src.general_hex_exchange import GeneralHexExchange
from src.general_hex_runtime import prepare_hex_authority
from src.project_hex import (get_project, inspect_project_policy, HexResolutionError,
    list_projects, register_project, resolve_hex, activate_hex)


def setup_general_hex_exchange_routes(*, db_path_resolver, owner_resolver=_owner, workspace_resolver=None, workspace_lister=None):
    router=APIRouter(prefix='/api/hexes',tags=['hex-exchange'])

    def authority():
        try:
            return prepare_hex_authority(db_path_resolver())
        except (GeneralHexError,sqlite3.Error,OSError) as exc:
            raise HTTPException(503,'General Hex authority unavailable: pinned local Memery required') from exc

    def invoke(operation, **values):
        try:
            return operation(**values)
        except GeneralHexConflict as exc:
            raise HTTPException(409,str(exc)) from exc
        except GeneralHexNotFound as exc:
            raise HTTPException(404,str(exc)) from exc
        except HexResolutionError as exc:
            raise HTTPException(409,str(exc)) from exc
        except (GeneralHexError,TypeError) as exc:
            raise HTTPException(422,str(exc)) from exc

    def fields(payload, allowed):
        if set(payload)-set(allowed):
            raise HTTPException(422,'unsupported fields')
        return dict(payload)

    @router.get('/contexts')
    def contexts(request: Request, task_id: str | None = None, offset: int = 0, limit: int = 100, search: str = ''):
        owner = owner_resolver(request); path = authority()
        from src.project_hex import ensure_policy_schema
        ensure_policy_schema(path)
        offset = max(0, int(offset)); limit = min(200, max(1, int(limit)))
        if workspace_lister:
            try:
                catalog = workspace_lister(request, offset, limit, str(search or '').strip())
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(503, 'FilePolicy workspace discovery is unavailable') from exc
            workspaces = list(catalog.get('workspaces') or [])
            total = int(catalog.get('total') or 0)
            next_offset = catalog.get('next_offset')
        else:
            # Compatibility for callers that have not wired the canonical
            # FilePolicy catalog yet. The app injects its owner-scoped catalog.
            registered = list_projects(owner=owner, db_path=path)
            needle = str(search or '').strip().casefold()
            if needle:
                registered = [item for item in registered if needle in item['canonical_root'].casefold()
                              or needle in item['project_id'].casefold()]
            page = registered[offset:offset + limit]
            workspaces = [dict(workspace_id=item['workspace_id'], name=item['canonical_root'],
                               path=item['canonical_root'], purpose='agent_workspace') for item in page]
            total = len(registered)
            next_offset = offset + len(workspaces) if offset + len(workspaces) < total else None
        all_projects = list_projects(owner=owner, db_path=path)
        with sqlite3.connect(path, timeout=30) as conn:
            for item in workspaces:
                workspace_id = str(item.get('workspace_id') or item.get('id') or '')
                canonical_root = str(item.get('path') or item.get('canonical_root') or '')
                registered = next((project for project in all_projects
                                   if project['workspace_id'] == workspace_id
                                   and canonical_root
                                   and project['canonical_root'] == str(Path(canonical_root).resolve())), None)
                item['workspace_id'] = workspace_id
                item['project_id'] = registered['project_id'] if registered else None
                item['canonical_root'] = canonical_root
                details = inspect_project_policy(registered['project_id'], owner=owner, db_path=path) if registered else None
                stored = (details or {}).get('projection')
                projection = dict(state=stored['state'], contract_hash=stored['contract_hash']) if stored else None
                item['policy'] = projection
                item['activation_state'] = (
                    'unavailable' if item.get('availability') == 'unavailable'
                    else projection['state'] if projection else 'not_activated'
                )
            row = conn.execute("SELECT snapshot_json FROM fm_general_hex_snapshots WHERE owner_id=? AND json_extract(snapshot_json,'$.context_tags.task_tags.context_id')=? ORDER BY rowid DESC LIMIT 1", (owner, task_id)).fetchone() if task_id else None
        pinned = json.loads(row[0]) if row else None
        if pinned and pinned['owner_id'] != owner:
            pinned['historical_owner_id'] = pinned['owner_id']; pinned['owner_id'] = owner
        return {'workspaces': workspaces, 'offset': offset, 'limit': limit, 'total': total,
                'next_offset': next_offset, 'coverage': {'kind': 'paginated', 'total': total, 'complete': next_offset is None},
                'latest_snapshot': pinned}

    def workspace_root(request: Request, workspace_id: str):
        if not workspace_resolver:
            raise HTTPException(503, 'Workspace authorization resolver is unavailable')
        try:
            return str(workspace_resolver(request, workspace_id))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, 'Selected workspace is unavailable for Hex inspection') from exc

    @router.post('/workspace/inspect')
    def inspect_workspace(request: Request, payload: dict = Body(...)):
        values = fields(payload, ('workspace_id',))
        workspace_id = str(values.get('workspace_id') or '').strip()
        if not workspace_id:
            raise HTTPException(422, 'workspace_id is required')
        owner = owner_resolver(request); path = authority(); root = workspace_root(request, workspace_id)
        try:
            # The selected FilePolicy workspace is the resolution boundary;
            # never inherit a contract from an ancestor workspace.
            resolution = resolve_hex(root, workspace_root=root)
        except HexResolutionError as exc:
            return {'workspace_id': workspace_id, 'project_id': None, 'state': 'unavailable',
                    'contract_path': None, 'contract_hash': None, 'contract': None, 'diagnostics': [str(exc)]}
        project = next((item for item in list_projects(owner=owner, db_path=path)
                        if item['canonical_root'] == str(Path(root).resolve())), None)
        details = None
        if resolution.contract_path is None:
            state = 'absent'
        elif project is None:
            state = 'discovered'
        else:
            details = inspect_project_policy(project['project_id'], owner=owner, db_path=path)
            state = (details.get('projection') or {}).get('state') or 'not_activated'
        return {'workspace_id': workspace_id, 'project_id': project['project_id'] if project else None,
                'state': state, 'contract_path': resolution.contract_path, 'contract_hash': resolution.contract_hash,
                'contract': resolution.contract, 'diagnostics': list(resolution.diagnostics) +
                    ([details['diagnostic']] if details and details.get('diagnostic') else []),
                'executable_policy': (details or {}).get('executable_policy')}

    def evolve_human(request: Request, workspace_id: str):
        from src.auth_helpers import require_user, is_internal_tool_identity
        if (getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False)
                or request.headers.get('authorization') or request.headers.get('x-odysseus-internal-token')):
            raise HTTPException(403, 'Workspace mode requires a signed-in human')
        owner = require_user(request)
        if is_internal_tool_identity(owner):
            raise HTTPException(403, 'Workspace mode requires a signed-in human')
        auth = getattr(request.app.state, 'auth_manager', None)
        account = auth.account_id(owner) if auth else None
        capture = getattr(request.app.state, 'capture_original_credential', None)
        if not account or auth.username_for_account_id(account) != owner or not callable(capture):
            raise HTTPException(401, 'Current account unavailable')
        try:
            current = capture(request, owner, account)
            if not callable(current) or not current(): raise ValueError('credential changed')
        except Exception as exc:
            raise HTTPException(401, 'Current credential unavailable') from exc
        root = workspace_root(request, workspace_id)
        path = authority()
        project = next((p for p in list_projects(owner=owner, db_path=path)
                        if p['workspace_id'] == workspace_id and p['canonical_root'] == str(Path(root).resolve())), None)
        if not project:
            raise HTTPException(404, 'Registered workspace not found')
        return owner, account, project, path, current

    @router.post('/workspace/evolve-mode')
    def workspace_evolve_mode(request: Request, payload: dict = Body(...)):
        values = fields(payload, ('workspace_id', 'mode', 'expected_revision'))
        workspace_id = str(values.get('workspace_id') or '').strip()
        if not workspace_id: raise HTTPException(422, 'workspace_id is required')
        owner, account, project, path, current = evolve_human(request, workspace_id)
        from src.project_hex import get_evolve_mode, set_evolve_mode
        try:
            if 'mode' not in values:
                result = get_evolve_mode(owner=owner, account_id=account, project_id=project['project_id'], db_path=path)
            else:
                result = set_evolve_mode(owner=owner, account_id=account, project_id=project['project_id'], mode=values['mode'], expected_revision=values.get('expected_revision'), current=current, db_path=path)
            if not current(): raise ValueError('Credential changed')
            if 'mode' not in values:
                from routes.prefs_routes import workspace_hex_operation
                operation = workspace_hex_operation(owner, account, project['project_id'])
                if operation:
                    result = dict(result, operation={
                        'operation_id': operation.get('operation_id'),
                        'state': operation.get('state'),
                        'contract_hash': operation.get('installed_contract_hash'),
                        'history_action_id': operation.get('action_id'),
                        'reason': str(operation.get('reason') or '')[:4000],
                        'trigger_evidence': 'agent_proposal_unverified',
                    })
            if not current(): raise ValueError('Credential changed')
            return result
        except (ValueError, HexResolutionError) as exc:
            raise HTTPException(409, str(exc)) from exc

    def human_evolve_rollback_context(request: Request, workspace_id: str, operation_id: str, *, recovering: bool = False):
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        from src.openclank.history_capture import HistoryContext
        from routes.prefs_routes import workspace_hex_mode, workspace_hex_operation
        owner, account, project, path, credential = evolve_human(request, workspace_id)
        state = request.app.state
        auth = getattr(state, 'auth_manager', None)
        repository = getattr(state, 'files_policy_repository', None)
        history = getattr(state, 'history_supervisor', None)
        if not repository or not history: raise HTTPException(503, 'Rollback authority unavailable')
        op = workspace_hex_operation(owner, account, project['project_id'])
        mode = workspace_hex_mode(owner, account, project['project_id'])
        valid_states = {'rollback_prepared', 'rollback_incomplete', 'rollback_recovering'} if recovering else {'complete'}
        if (not op or op.get('operation_id') != operation_id or op.get('state') not in valid_states
                or (recovering and (op.get('rollback_actor_id') != owner or op.get('rollback_actor_kind') != 'human'))
                or mode != {'mode':'evolve','revision':op.get('mode_revision')}):
            raise HTTPException(409, 'Evolve operation is stale or unavailable')
        try:
            workspace = resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                                                 auth_manager=auth, purpose='app_folder')
            if str(Path(workspace.path).resolve()) != project['canonical_root']: raise ValueError('workspace changed')
            location = repository.get_location(workspace.workspace.location_id)
            resolution = resolve_hex(workspace.path, workspace_root=workspace.path)
            if not resolution.contract_path: raise ValueError('contract unavailable')
            targets = [Path(resolution.contract_path), Path(workspace.path) / 'AGENTS.md']
            relative = [str(target.relative_to(Path(location.canonical_path))).replace('\\','/') for target in targets]
            generation = repository.generation()
            def current():
                try:
                    if (not credential() or getattr(request.app.state,'auth_manager',None) is not auth
                            or getattr(request.app.state,'files_policy_repository',None) is not repository
                            or getattr(request.app.state,'history_supervisor',None) is not history
                            or str(db_path_resolver()) != path or repository.generation() != generation
                            or get_project(project['project_id'],owner=owner,db_path=path) != project): return False
                    fresh = resolve_owned_workspace(repository, workspace_id=workspace_id, owner_username=owner,
                                                    auth_manager=auth, purpose='app_folder')
                    if fresh.workspace != workspace.workspace or fresh.path != workspace.path: return False
                    return all(repository.resolve(subject_id=account, is_admin=bool(auth.is_admin(owner)), origin='app',
                        location_id=location.id, capability='write', workspace_id=workspace_id, relative_resource=target,
                        observation_only=True).allowed for target in relative)
                except Exception: return False
            if not current(): raise ValueError('workspace write authority unavailable')
            eligible = []
            for binding in history.authorized_roots:
                try:
                    base = Path(binding['canonical_path']).resolve()
                    Path(workspace.path).resolve().relative_to(base)
                    if account not in (binding.get('account_ids') or ()): continue
                    if binding.get('actor_ids') and owner not in binding['actor_ids']: continue
                    if binding.get('workspace_ids') and workspace_id not in binding['workspace_ids']: continue
                    eligible.append((len(base.parts), str(binding['root_id']), str(base)))
                except (KeyError, TypeError, ValueError, OSError): continue
            if not eligible: raise ValueError('History authority unavailable')
            _, root_id, root_path = max(eligible)
            base_current = current
            def current():
                return base_current() and any(str(item.get('root_id')) == root_id and str(Path(item.get('canonical_path') or '').resolve()) == root_path
                    and account in (item.get('account_ids') or ()) and (not item.get('actor_ids') or owner in item['actor_ids'])
                    and (not item.get('workspace_ids') or workspace_id in item['workspace_ids']) for item in history.authorized_roots)
            if not current(): raise ValueError('History authority changed')
            context = HistoryContext(actor_id=owner, account_id=account, workspace_id=workspace_id,
                client=history.client_for(owner, account), actor_kind='human', tool_id='evolve-rollback',
                root_bindings=((root_id,root_path),))
            return owner, account, project, path, op, context, current, targets
        except HTTPException: raise
        except Exception as exc: raise HTTPException(503, 'Rollback authority unavailable') from exc

    @router.post('/workspace/evolve-rollback-preview')
    def preview_evolve_rollback(request: Request, payload: dict = Body(...)):
        import difflib, hashlib
        values = fields(payload, ('workspace_id','operation_id'))
        workspace_id, operation_id = values.get('workspace_id'), values.get('operation_id')
        if not isinstance(workspace_id,str) or not workspace_id or not isinstance(operation_id,str) or not operation_id:
            raise HTTPException(422, 'Workspace and operation are required')
        owner, account, project, path, op, context, current, targets = human_evolve_rollback_context(request,workspace_id,operation_id)
        try:
            from src.project_hex import _read_evolve_originals, _evolve_agents_bytes, _evolve_optional_hash
            projection = inspect_project_policy(project['project_id'],owner=owner,db_path=path).get('projection') or {}
            current_contract = targets[0].read_bytes()
            if (projection.get('state') != 'active' or projection.get('source_revision') != op.get('installed_policy_revision')
                    or hashlib.sha256(current_contract).hexdigest() != op.get('installed_contract_hash')
                    or _evolve_optional_hash(_evolve_agents_bytes(targets[1])) != op.get('installed_agents_hash')):
                raise ValueError('Workspace changed since Evolve operation')
            original_contract, original_agents = _read_evolve_originals(context.client,op,account_id=account,actor_id=owner,actor_kind='human')
            if (not isinstance(original_contract,bytes) or hashlib.sha256(original_contract).hexdigest() != op.get('old_contract_hash')
                    or _evolve_optional_hash(original_agents) != op.get('old_agents_hash') or not current()):
                raise ValueError('Original History evidence unavailable')
            diff = ''.join(difflib.unified_diff(current_contract.decode('utf-8').splitlines(keepends=True),
                original_contract.decode('utf-8').splitlines(keepends=True),fromfile='current contract',tofile='original contract'))
            if len(diff) > 524288: raise ValueError('Rollback diff exceeds review limit')
            return {'operation_id':operation_id,'diff':diff,'restores_agents_absence':original_agents is None,
                    'trigger_evidence':'agent_proposal_unverified','reason':str(op.get('reason') or '')[:4000]}
        except Exception as exc:
            raise HTTPException(409, 'Rollback preview unavailable or workspace changed') from exc

    @router.post('/workspace/evolve-rollback')
    def human_evolve_rollback(request: Request, payload: dict = Body(...)):
        values = fields(payload, ('workspace_id','operation_id'))
        workspace_id, operation_id = values.get('workspace_id'), values.get('operation_id')
        if not isinstance(workspace_id,str) or not workspace_id or not isinstance(operation_id,str) or not operation_id:
            raise HTTPException(422, 'Workspace and operation are required')
        owner, account, project, path, op, context, current, _ = human_evolve_rollback_context(request,workspace_id,operation_id)
        try:
            from src.project_hex import rollback_evolve_candidate
            return rollback_evolve_candidate(owner=owner,account_id=account,project_id=project['project_id'],
                actor_id=owner,operation_id=operation_id,history_context=context,current=current,db_path=path)
        except Exception as exc:
            raise HTTPException(409, 'Rollback unavailable or workspace changed') from exc

    @router.post('/workspace/evolve-rollback-reconcile')
    def human_evolve_rollback_reconcile(request: Request, payload: dict = Body(...)):
        values = fields(payload, ('workspace_id','operation_id'))
        workspace_id, operation_id = values.get('workspace_id'), values.get('operation_id')
        if not isinstance(workspace_id,str) or not workspace_id or not isinstance(operation_id,str) or not operation_id:
            raise HTTPException(422, 'Workspace and operation are required')
        owner, account, project, path, op, context, current, _ = human_evolve_rollback_context(request,workspace_id,operation_id,recovering=True)
        try:
            from src.project_hex import reconcile_evolve_rollback
            return reconcile_evolve_rollback(owner=owner,account_id=account,project_id=project['project_id'],
                actor_id=owner,operation_id=operation_id,history_context=context,current=current,db_path=path)
        except Exception as exc:
            raise HTTPException(409, 'Rollback recovery unavailable or workspace changed') from exc

    @router.post('/workspace/activate')
    def activate_workspace(request: Request, payload: dict = Body(...)):
        values = fields(payload, ('workspace_id', 'expected_contract_hash'))
        workspace_id = str(values.get('workspace_id') or '').strip()
        expected_hash = str(values.get('expected_contract_hash') or '').strip()
        if not workspace_id or not expected_hash:
            raise HTTPException(422, 'workspace_id and expected_contract_hash are required')
        owner = _human_change(request, owner_resolver); path = authority(); root = workspace_root(request, workspace_id)
        resolution = resolve_hex(root, workspace_root=root)
        if not resolution.contract_hash or resolution.contract_hash != expected_hash:
            raise HTTPException(409, 'Workspace contract changed after review; inspect it again before activation')
        try:
            project = register_project(root, owner=owner, workspace_id=workspace_id, db_path=path)
            return activate_hex(resolution, owner=owner, project_id=project['project_id'], db_path=path, actor_id=owner)
        except HexResolutionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get('/preview')
    def preview(request:Request, project_id:str|None=None, workspace_id:str|None=None, task_id:str|None=None, budget_chars:int=24000):
        owner=owner_resolver(request);path=authority();workspace=None
        if project_id:
            project=get_project(project_id,owner=owner,db_path=path)
            if not project:
                raise HTTPException(404,'owner project not found')
            workspace=project['canonical_root'];workspace_id=workspace_id or project['workspace_id']
        return invoke(GeneralHexComposition(path).preview,owner_id=owner,workspace=workspace,workspace_id=workspace_id,task_id=task_id,budget_chars=budget_chars)

    @router.get('/workspace/{project_id}')
    def workspace(request:Request,project_id:str):
        return invoke(inspect_project_policy,project_id=project_id,owner=owner_resolver(request),db_path=authority())

    @router.post('/promote/preview')
    def promote_preview(request:Request,payload:dict=Body(...)):
        return invoke(GeneralHexExchange(authority()).preview_promotion,owner_id=owner_resolver(request),**fields(payload,('project_id','rule_index')))

    @router.post('/promote/save')
    def promote_save(request:Request,payload:dict=Body(...)):
        return invoke(GeneralHexExchange(authority()).save_promotion,owner_id=_human_change(request, owner_resolver),**fields(payload,('draft_id','expected_draft_revision','title','body','tags','enabled')))

    @router.get('/general/{hex_id}/export')
    def export(request:Request,hex_id:str,revision:int|None=None):
        payload=invoke(GeneralHexExchange(authority()).export,owner_id=owner_resolver(request),hex_id=hex_id,revision=revision)
        # Browser downloads are explicit copies. This route never reads or
        # writes arbitrary host paths; Files/Lore remains the file authority.
        return Response(json.dumps(payload,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="general-hex-{hex_id}.json"'})

    @router.post('/import')
    def import_copy(request:Request,payload:dict=Body(...)):
        values=fields(payload,('document','accept_assisted'))
        if type(values.get('accept_assisted',False)) is not bool:
            raise HTTPException(422,'accept_assisted must be boolean')
        return invoke(GeneralHexExchange(authority()).import_payload,owner_id=_human_change(request, owner_resolver),payload=values.get('document'),accept_assisted=values.get('accept_assisted',False))

    @router.post('/apply/preview')
    def apply_preview(request:Request,payload:dict=Body(...)):
        return invoke(GeneralHexExchange(authority()).preview_application,owner_id=owner_resolver(request),**fields(payload,('hex_id','revision','project_id','scope_in','scope_except')))

    @router.post('/apply')
    def apply(request:Request,payload:dict=Body(...)):
        return invoke(GeneralHexExchange(authority()).apply,owner_id=_human_change(request, owner_resolver),**fields(payload,('draft_id','expected_draft_revision')))

    return router
