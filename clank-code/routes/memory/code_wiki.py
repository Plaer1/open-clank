"""Structure-only code wiki routes (plan and page). No model call is reachable from here."""
import json
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

_HEADERS = {'Cache-Control': 'private, no-store'}
_PLAN_CAP, _PAGE_CAP = 4096, 8192
_PAGE_KEYS = {'page_id', 'file_id', 'rel_path', 'bytes_est'}


def register_code_wiki(router, *, require_user, provider, query_code=None, current_admission=None):
    def fail(status, message):
        return HTTPException(status, message, headers=_HEADERS)

    async def read_body(request, cap):
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > cap: raise fail(413, 'Wiki request exceeds its bound')
            payload.extend(chunk)
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result: raise ValueError('duplicate key')
                result[key] = value
            return result
        return json.loads(bytes(payload).decode('utf-8', 'strict'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON')))

    async def execute(request: Request, action: str):
        owner = require_user(request)
        # Same original-credential capture and currentness bracket as the code-summary routes.
        admission_source = current_admission
        if admission_source is None:
            from src.openclank.account_request_barrier import _current_admission
            admission_source = _current_admission.get
        original_admission = admission_source(); app_state = request.app.state
        original_barrier = getattr(app_state, 'account_request_barrier', None)
        original_auth = getattr(app_state, 'auth_manager', None)
        original_factory = getattr(app_state, 'capture_original_credential', None)
        original_account = None; original_credential = None
        try:
            if (getattr(request.state, 'authenticated', False) is True and not getattr(request.state, 'api_token', False)
                    and not getattr(request.state, 'internal_tool_authenticated', False) and callable(original_factory)
                    and original_auth is not None and original_barrier is not None):
                original_account = original_auth.account_id(owner)
                original_credential = original_factory(request, owner, original_account)
        except Exception:
            original_credential = None

        def current():
            try:
                original_barrier.assert_live_admission(owner, original_admission)
                return (getattr(app_state, 'auth_manager', None) is original_auth
                    and getattr(app_state, 'account_request_barrier', None) is original_barrier
                    and getattr(app_state, 'capture_original_credential', None) is original_factory
                    and callable(original_credential) and original_credential() is True
                    and original_auth.account_id(owner) == original_account
                    and original_auth.username_for_account_id(original_account) == owner)
            except Exception:
                return False
        if not current(): raise fail(409, 'Wiki source or authority is unavailable or changed')
        body = await read_body_checked(request, _PLAN_CAP if action == 'plan' else _PAGE_CAP)
        try:
            from src.openclank import code_wiki
            repository = getattr(app_state, 'files_policy_repository', None)
            if repository is None or original_auth is None: raise RuntimeError('Authority unavailable')
            if not isinstance(body, dict) or type(body.get('workspace_id')) is not str or not 1 <= len(body['workspace_id'].encode()) <= 256:
                raise ValueError('Invalid wiki request')
            workspace_id = body['workspace_id']
            call = query_code
            if call is None:
                from src.openclank.code_query import query_code as call

            async def query(q):
                return await call(repository=repository, provider=provider, auth_manager=original_auth, owner=owner,
                                  workspace_id=workspace_id, purpose='app_folder', session=None, query=q)
            document_workspace = body.get('copal_workspace')
            if 'copal_workspace' in body and (type(document_workspace) is not str or not 1 <= len(document_workspace) <= 128):
                raise ValueError('Invalid citation workspace')
            documents = None

            async def document_citations(receipt, resource, outline=None):
                nonlocal documents
                from routes.copal_routes import _bridge, _call, _doc_id, _note_view, _scope
                from src.openclank.document_code_links import reverse
                from src.openclank.document_journeys import catalogue
                if not current():
                    raise ValueError('Original request authority changed')
                scope = _scope(request, document_workspace)
                async def current_document(ident):
                    return _note_view(await _call(request, 'get', {**scope, 'id':_doc_id(ident)}))
                try:
                    if documents is None:
                        documents = await catalogue(_bridge(request), scope)
                    return await reverse(request, owner, {"codeWorkspaceId":workspace_id, "codeReceipt":receipt,
                        "resourceId":resource}, documents, current_document=current_document, module_outline=outline)
                except (ValueError, HTTPException):
                    return {"citations":[], "coverage":{"complete":False}}

            if action == 'plan':
                if 'copal_workspace' in body and body.get('communities') is not True:
                    raise ValueError('Document scope needs community pages')
                async def community_citations(receipt, nodes):
                    node = next((item for item in nodes if item.get("kind") == "declaration"
                                 and (item.get("properties") or {}).get("identity_state") != "ambiguous_snapshot_local"), nodes[0])
                    return await document_citations(receipt, node["id"])
                plan_body = {key:value for key,value in body.items() if key != 'copal_workspace'}
                result = await code_wiki.plan(plan_body, query=query,
                    community_citations=community_citations if document_workspace else None)
                result = {**result, 'index_markdown': code_wiki.render_index(result)}
            else:
                page, receipt = body.get('page'), body.get('receipt')
                if (set(body) not in ({'workspace_id', 'receipt', 'page'}, {'workspace_id', 'receipt', 'page', 'copal_workspace'})
                        or not isinstance(page, dict) or set(page) != _PAGE_KEYS
                        or any(type(page[k]) is not str for k in ('page_id', 'file_id', 'rel_path')) or type(page['bytes_est']) is not int
                        or code_wiki._check_receipt(receipt)['workspace_id'] != workspace_id):
                    raise ValueError('Invalid wiki request')
                async def citations(page, outline):
                    return await document_citations(receipt, page["file_id"], outline)
                markdown = await code_wiki.fetch_page(page, query=query, receipt=receipt,
                                                      citations=citations if document_workspace else None)
                result = {'page_id': page['page_id'], 'markdown': markdown}
            if not current(): raise ValueError('Original request authority changed')
            return JSONResponse(result, headers=_HEADERS)
        except (ValueError, TypeError, UnicodeDecodeError):
            raise fail(409, 'Wiki source, receipt or request is unavailable or changed') from None
        except HTTPException:
            raise
        except Exception:
            raise fail(503, 'Wiki operation unavailable') from None

    async def read_body_checked(request, cap):
        try:
            return await read_body(request, cap)
        except HTTPException:
            raise
        except Exception:
            raise fail(409, 'Wiki source, receipt or request is unavailable or changed') from None

    async def wiki_plan(request: Request): return await execute(request, 'plan')
    async def wiki_page(request: Request): return await execute(request, 'page')
    router.add_api_route('/code/wiki/plan', wiki_plan, methods=['POST'])
    router.add_api_route('/code/wiki/page', wiki_page, methods=['POST'])
