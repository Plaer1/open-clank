"""Read-only memory explorer figures: counts, growth, newest changes, one record's links. Titles and ids only."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
_HEADERS = {'Cache-Control': 'private, no-store'}
_LEGACY_BOUND = 5000
UNAVAILABLE_ON_LEGACY = ['by_status', 'links', 'superseded_and_forgotten_events', 'domains']


def _legacy_figures(rows, days):
    """What the legacy reader can give: kind/category counts, creation per day, newest records. No titles exist."""
    by_kind, per_day, stamped = {}, {}, []
    floor = datetime.now(timezone.utc).timestamp() - days * 86400
    for r in rows:
        kind = str(r.get('kind') or r.get('category') or 'unknown')
        by_kind[kind] = by_kind.get(kind, 0) + 1
        ts = r.get('timestamp')
        if isinstance(ts, (int, float)) and ts > 0:
            stamped.append((ts, r))
            if ts >= floor:
                day = datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d')
                per_day[day] = per_day.get(day, 0) + 1
    stamped.sort(key=lambda x: -x[0])
    recent = [{'block_id': str(r.get('id')), 'title': None, 'kind': str(r.get('kind') or r.get('category') or 'unknown'),
               'event': 'created', 'revision': 1, 'at': datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
              for ts, r in stamped[:20]]
    return {'version': 1, 'state': 'legacy', 'truncated': len(rows) >= _LEGACY_BOUND, 'total': len(rows), 'by_kind': by_kind,
            'growth': [{'day': d, 'created': per_day[d]} for d in sorted(per_day)], 'recent': recent,
            'unavailable': UNAVAILABLE_ON_LEGACY}


def register_memory_explorer(router, *, require_human, capture_context, owned, provider):
    def unavailable(status=503):
        return JSONResponse({'state': 'unavailable'}, status_code=status, headers=_HEADERS)

    def legacy_context(user):
        from src.memory_scope import memory_owner
        @asynccontextmanager
        async def admitted(_user):
            yield
        return {'owner': memory_owner(user), 'barrier': SimpleNamespace(admitted=admitted), 'current_live': lambda: True}

    async def explorer(request: Request, days: int = Query(90, ge=1, le=366), block: str = Query(None, max_length=512), session: str = Query(None, max_length=256)):
        try:
            user = require_human(request)
            context = capture_context(request, user) if getattr(provider, '_guarded_v17', False) is True else legacy_context(user)
        except Exception:
            return unavailable(403)
        try:
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                figures = None
                db = getattr(provider, '_local_db_path', lambda: None)()
                if db is not None:
                    from src.memery_v2 import explorer_overview
                    scope = provider._scope(context['owner'])
                    figures = await asyncio.to_thread(explorer_overview, owner=scope.owner, workspace_id=scope.workspace_id,
                                                      days=days, block_id=block, db_path=db)
                if figures is None or figures.get('state') != 'ready':
                    rows = [r if isinstance(r, dict) else getattr(r, '__dict__', {}) for r in
                            await provider.list_memories(owner=context['owner'], limit=_LEGACY_BOUND)]
                    rows = [r for r in rows if r.get('owner', context['owner']) == context['owner']]
                    figures = _legacy_figures(rows, days)
                if block and figures and figures.get('state') == 'ready' and figures.get('links', {}).get('state') == 'ready':
                    # A link is only a selector. Re-read the active scoped record
                    # rather than treating a retained/forgotten block as current.
                    from src.memery_v2 import list_current_records
                    scope = provider._scope(context['owner'])
                    records = await asyncio.to_thread(list_current_records, owner=scope.owner, workspace_id=scope.workspace_id,
                        project_id=getattr(scope,'project_id',None), identity=block, limit=5, _raise_errors=True, db_path=db)
                    selected = [row for row in records if isinstance(row,dict) and isinstance(row.get('metadata'),dict) and row['metadata'].get('block_id') == block]
                    if len(selected) != 1:
                        figures['links'] = {'state':'unavailable'}
                    elif session and getattr(provider, '_guarded_v17', False) is True:
                        from src.openclank.workflow_memory_links import current_plan_references
                        selected_revision = selected[0]['metadata'].get('revision')
                        refs = await current_plan_references(request,context,session_id=session,block_id=block,owned=owned)
                        after = await asyncio.to_thread(list_current_records, owner=scope.owner, workspace_id=scope.workspace_id,
                            project_id=getattr(scope,'project_id',None), identity=block, limit=5, _raise_errors=True, db_path=db)
                        still = [row for row in after if isinstance(row,dict) and isinstance(row.get('metadata'),dict) and row['metadata'].get('block_id') == block]
                        if len(still)!=1 or still[0]['metadata'].get('revision') != selected_revision:
                            figures['links'] = {'state':'unavailable'}
                        else:
                            figures['current_plan_references'] = refs
                    elif session:
                        figures['current_plan_references'] = {'complete':False,'items':[]}
                if not await owned(context['current_live']): raise ValueError('authority changed')
            return JSONResponse(figures, headers=_HEADERS)
        except Exception as exc:
            logger.warning('Memory explorer failed: %s: %s', type(exc).__name__, str(exc)[:120])
            return unavailable()

    router.add_api_route('/explorer', explorer, methods=['GET'])
