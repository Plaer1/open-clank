"""Read-only scoped filter + exact count for the memory panel (contract row "scope/filter/count/clear", count half).

`GET /api/memory/count?kind=&status=&predicate=&entity=&created_after=&created_before=` returns
`{eligible_count, filters}`: the exact number of current records the panel list would show over all pages for the
authenticated owner and the server workspace, narrowed by a CLOSED set of filters. The count runs the list's own
read (src/memery_v2.count_current_records), so scope, lineage/archived/trust visibility and de-duplication
are the list's, never a second copy. No Native operation or capability string exists for it.

"Clear" is NOT an operation here: the contract assigns it to the existing explicit reset lifecycle, and nothing
in this module writes or deletes.
"""
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
_HEADERS = {'Cache-Control': 'private, no-store'}
FILTER_NAMES = ('kind', 'status', 'predicate', 'entity', 'created_after', 'created_before')


def register_native_ops_read(router, *, require_human, capture_context, owned, provider):
    """Guarded profile: the paired named-human bracket (``capture_context``). Legacy profile has no paired authority
    (``_guarded_preparation`` is None), so it gets the plain named-user scoping `GET /api/memory` uses."""
    def refuse(status, state):
        return JSONResponse({'state': state}, status_code=status, headers=_HEADERS)

    def legacy_context(user):
        from src.memory_scope import memory_owner
        @asynccontextmanager
        async def admitted(_user):
            yield
        return {'owner': memory_owner(user), 'barrier': SimpleNamespace(admitted=admitted), 'current_live': lambda: True}

    async def count(request: Request):
        try:
            user = require_human(request)
            context = capture_context(request, user) if getattr(provider, '_guarded_v17', False) is True else legacy_context(user)
        except Exception:
            return refuse(403, 'unavailable')
        query = request.query_params
        filters = {}
        for key in query.keys():
            values = query.getlist(key)
            if key not in FILTER_NAMES or len(values) != 1:
                return refuse(422, 'invalid_filter')
            filters[key] = values[0]
        try:
            from src.memery_v2 import _validate_record_filters
            _validate_record_filters(filters)
        except ValueError:
            return refuse(422, 'invalid_filter')
        try:
            counter = getattr(provider, 'count_v2_records', None)
            if not callable(counter):
                return refuse(503, 'unavailable')
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                total = await counter(owner=context['owner'], filters=filters or None)
                if not await owned(context['current_live']): raise ValueError('authority changed')
            if type(total) is not int or total < 0:
                return refuse(503, 'unavailable')
            return JSONResponse({'eligible_count': total, 'filters': filters}, headers=_HEADERS)
        except Exception as exc:
            # Class and a short message only: never memory text, owner or paths.
            logger.warning('Memory count failed: %s: %s', type(exc).__name__, str(exc)[:120])
            return refuse(503, 'unavailable')

    async def search_receipt(request: Request):
        """Lexical search over the owner's current records with the component receipt (vectors are reported unavailable)."""
        try:
            user = require_human(request)
            context = capture_context(request, user) if getattr(provider, '_guarded_v17', False) is True else legacy_context(user)
        except Exception:
            return refuse(403, 'unavailable')
        query = request.query_params
        filters, text, limit = {}, None, 10
        for key in query.keys():
            values = query.getlist(key)
            if len(values) != 1:
                return refuse(422, 'invalid_filter')
            if key == 'q':
                text = values[0]
            elif key == 'limit':
                if not values[0].isascii() or not values[0].isdigit() or not 1 <= int(values[0]) <= 100:
                    return refuse(422, 'invalid_filter')
                limit = int(values[0])
            elif key in FILTER_NAMES:
                filters[key] = values[0]
            else:
                return refuse(422, 'invalid_filter')
        if not text or len(text) > 512 or not text.strip():
            return refuse(422, 'invalid_filter')
        try:
            from src.memery_v2 import _validate_record_filters
            _validate_record_filters(filters)
        except ValueError:
            return refuse(422, 'invalid_filter')
        try:
            searcher = getattr(provider, 'search_v2_with_receipt', None)
            if not callable(searcher):
                return refuse(503, 'unavailable')
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                found = await searcher(text, owner=context['owner'], top_k=limit, filters=filters or None)
                if not await owned(context['current_live']): raise ValueError('authority changed')
            if not (isinstance(found, tuple) and len(found) == 2 and isinstance(found[0], list) and isinstance(found[1], dict)):
                return refuse(503, 'unavailable')
            rows, receipt = found
            items = [{'id': r.get('id'), 'text': r.get('text'), 'kind': r.get('kind'), 'status': r.get('status'),
                      'predicate': (r.get('metadata') or {}).get('predicate'), 'tags': r.get('tags') or [],
                      'score': r.get('recall_score')} for r in rows if r.get('owner', context['owner']) == context['owner']]
            return JSONResponse({'items': items, 'receipt': receipt, 'filters': filters}, headers=_HEADERS)
        except Exception as exc:
            logger.warning('Memory search receipt failed: %s: %s', type(exc).__name__, str(exc)[:120])
            return refuse(503, 'unavailable')

    async def entities(request: Request):
        """Scoped alias / entity-type lookup: `?alias=&type=&limit=1..100` -> `{entities, receipt}`."""
        try:
            user = require_human(request)
            context = capture_context(request, user) if getattr(provider, '_guarded_v17', False) is True else legacy_context(user)
        except Exception:
            return refuse(403, 'unavailable')
        query = request.query_params
        values = {}
        for key in query.keys():
            got = query.getlist(key)
            if key not in ('alias', 'type', 'limit') or len(got) != 1:
                return refuse(422, 'invalid_filter')
            values[key] = got[0]
        limit = 20
        if 'limit' in values:
            if not values['limit'].isascii() or not values['limit'].isdigit() or not 1 <= int(values['limit']) <= 100:
                return refuse(422, 'invalid_filter')
            limit = int(values['limit'])
        alias, kind = values.get('alias'), values.get('type')
        if (alias is None and kind is None) or any(v is not None and (not v.strip() or len(v) > 512) for v in (alias, kind)):
            return refuse(422, 'invalid_filter')
        try:
            lookup = getattr(provider, 'entity_lookup_v2', None)
            if not callable(lookup):
                return refuse(503, 'unavailable')
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                found = await lookup(owner=context['owner'], alias=alias, entity_type=kind, limit=limit)
                if not await owned(context['current_live']): raise ValueError('authority changed')
            if not (isinstance(found, dict) and isinstance(found.get('entities'), list) and isinstance(found.get('receipt'), dict)):
                return refuse(503, 'unavailable')
            return JSONResponse({'entities': found['entities'], 'receipt': found['receipt']}, headers=_HEADERS)
        except Exception as exc:
            logger.warning('Entity lookup failed: %s: %s', type(exc).__name__, str(exc)[:120])
            return refuse(503, 'unavailable')

    async def related(request: Request, memory_id: str):
        """Related records. `mode=graph` walks typed edges (two hops, with a truncation receipt); `mode=semantic` is
        reported unavailable (vectors are disabled). A foreign or unknown id looks like "no related items"."""
        try:
            user = require_human(request)
            context = capture_context(request, user) if getattr(provider, '_guarded_v17', False) is True else legacy_context(user)
        except Exception:
            return refuse(403, 'unavailable')
        query = request.query_params
        filters, mode, limit = {}, None, 10
        for key in query.keys():
            got = query.getlist(key)
            if len(got) != 1:
                return refuse(422, 'invalid_filter')
            if key == 'mode':
                mode = got[0]
            elif key == 'limit':
                if not got[0].isascii() or not got[0].isdigit() or not 1 <= int(got[0]) <= 100:
                    return refuse(422, 'invalid_filter')
                limit = int(got[0])
            elif key in FILTER_NAMES:
                filters[key] = got[0]
            else:
                return refuse(422, 'invalid_filter')
        if mode not in ('graph', 'semantic') or not memory_id or len(memory_id) > 512 or memory_id != memory_id.strip():
            return refuse(422, 'invalid_filter')
        try:
            from src.memery_v2 import _validate_record_filters
            _validate_record_filters(filters)
        except ValueError:
            return refuse(422, 'invalid_filter')
        try:
            finder = getattr(provider, 'related_memories', None)
            if not callable(finder):
                return refuse(503, 'unavailable')
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                found = await finder(memory_id, owner=context['owner'], mode=mode, limit=limit, filters=filters or None)
                if not await owned(context['current_live']): raise ValueError('authority changed')
            if not isinstance(found, dict) or not isinstance(found.get('items'), list):
                return refuse(503, 'unavailable')
            return JSONResponse({**found, 'filters': filters}, headers=_HEADERS)
        except Exception as exc:
            logger.warning('Related memories failed: %s: %s', type(exc).__name__, str(exc)[:120])
            return refuse(503, 'unavailable')

    router.add_api_route('/count', count, methods=['GET'])
    router.add_api_route('/entities', entities, methods=['GET'])
    router.add_api_route('/{memory_id}/related', related, methods=['GET'])
    router.add_api_route('/search-receipt', search_receipt, methods=['GET'])
