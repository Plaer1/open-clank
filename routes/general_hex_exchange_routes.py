"""User-directed preview, promotion, file-copy exchange and workspace publication."""
import json
import sqlite3
from pathlib import Path
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response
from routes.general_hex_routes import _owner
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
            raise HTTPException(503,'General Hex authority unavailable: pinned local Frankenmemory required') from exc

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
                row = conn.execute("SELECT state,contract_hash FROM fm_v2_policy_projections WHERE owner_id=? AND project_id=?", (owner, registered['project_id'])).fetchone() if registered else None
                projection = dict(state=row[0], contract_hash=row[1]) if row else None
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
        if resolution.contract_path is None:
            state = 'absent'
        elif project is None:
            state = 'discovered'
        else:
            details = inspect_project_policy(project['project_id'], owner=owner, db_path=path)
            state = (details.get('projection') or {}).get('state') or 'not_activated'
        return {'workspace_id': workspace_id, 'project_id': project['project_id'] if project else None,
                'state': state, 'contract_path': resolution.contract_path, 'contract_hash': resolution.contract_hash,
                'contract': resolution.contract, 'diagnostics': list(resolution.diagnostics)}

    @router.post('/workspace/activate')
    def activate_workspace(request: Request, payload: dict = Body(...)):
        values = fields(payload, ('workspace_id', 'expected_contract_hash'))
        workspace_id = str(values.get('workspace_id') or '').strip()
        expected_hash = str(values.get('expected_contract_hash') or '').strip()
        if not workspace_id or not expected_hash:
            raise HTTPException(422, 'workspace_id and expected_contract_hash are required')
        owner = owner_resolver(request); path = authority(); root = workspace_root(request, workspace_id)
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
        return invoke(GeneralHexExchange(authority()).save_promotion,owner_id=owner_resolver(request),**fields(payload,('draft_id','expected_draft_revision','title','body','tags','enabled')))

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
        return invoke(GeneralHexExchange(authority()).import_payload,owner_id=owner_resolver(request),payload=values.get('document'),accept_assisted=values.get('accept_assisted',False))

    @router.post('/apply/preview')
    def apply_preview(request:Request,payload:dict=Body(...)):
        return invoke(GeneralHexExchange(authority()).preview_application,owner_id=owner_resolver(request),**fields(payload,('hex_id','revision','project_id','scope_in','scope_except')))

    @router.post('/apply')
    def apply(request:Request,payload:dict=Body(...)):
        return invoke(GeneralHexExchange(authority()).apply,owner_id=owner_resolver(request),**fields(payload,('draft_id','expected_draft_revision')))

    return router
