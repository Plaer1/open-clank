"""Friendly read-only code tool; existing app/broker owns all authority."""
import json
import os
from contextvars import ContextVar

_outline_invocation = ContextVar("source_outline_invocation", default=None)

def begin_source_outline_request(request):
    holder = {"active": True, "request": request, "sidecar": None}
    return holder, _outline_invocation.set(holder)

def end_source_outline_request(holder, token):
    holder["active"] = False
    _outline_invocation.reset(token)



class SearchCodeTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.openclank.code_query import CodeQueryError, finalize_error, query_code, validate_query, validate_library_query, unavailable_library, measure_envelope, DEFAULT_BUDGETS
        from src.memory_provider import MemoryRequestRejectedError
        from src import ai_interaction
        owner = str(ctx.get("owner") or "")
        sid = str(ctx.get("session_id") or "")
        workspace = str(ctx.get("authority_workspace_id") or "")
        query = {}
        try:
            raw=json.loads(content)
            if isinstance(raw,dict) and raw.get('operation')=='library_api':
                query={'operation':'library_api'}
                query=validate_library_query(raw)
            else:
                query=validate_query(raw)
        except (TypeError, ValueError, CodeQueryError):
            result = unavailable_library() if query.get('operation')=='library_api' else finalize_error({}, "invalid", "search_code requires a bounded code-query JSON object")
        else:
            provider = getattr(ai_interaction, "_memory_provider", None)
            try:
                if not owner or not sid or not workspace or provider is None:
                    raise CodeQueryError("denied", "Code chat workspace unavailable")
                if getattr(provider, "_broker_url", ""):
                    holder = _outline_invocation.get()
                    request = holder.get("request") if isinstance(holder, dict) and holder.get("active") is True else None
                    result = await provider.code_query_session(query=query, session_id=sid, authority_workspace_id=workspace,
                        **({"_outline_request": request} if request is not None else {}))
                    if request is not None and isinstance(result, dict) and set(result) == {"result", "outline"}:
                        if holder.get("active") is True and isinstance(result.get("outline"), dict): holder["sidecar"] = result["outline"]
                        result = result["result"]
                else:
                    if query.get('operation')=='library_api':raise CodeQueryError('denied','Authenticated code broker required')
                    from src.constants import AUTH_FILE
                    from src.tool_execution import _read_only_auth_snapshot
                    from src.openclank.file_policy import FilePolicyRepository
                    manager = ai_interaction.get_session_manager()
                    session = manager.get_session(sid) if manager is not None else None
                    result = await query_code(repository=FilePolicyRepository(), provider=provider,
                        auth_manager=_read_only_auth_snapshot(os.environ.get("OPEN_CLANK_AUTHORITY_AUTH_PATH") or AUTH_FILE),
                        owner=owner, workspace_id=workspace, purpose="agent_workspace", session=session, query=query)
            except CodeQueryError as exc:
                result = finalize_error(query, exc.code, "Code chat workspace unavailable")
            except MemoryRequestRejectedError:
                result = finalize_error(query, "denied", "Code chat workspace unavailable")
            except Exception:
                result = finalize_error(query, "unavailable", "Code query transport unavailable")
        if query.get('operation')=='library_api':
            if (type(result) is not dict or result.get('schema')!='open-clank.library-observation/v1'
                    or result.get('state') not in ('current','unavailable') or result.get('partial') is not True
                    or (result.get('state')=='unavailable' and set(result)!={'schema','state','partial'})
                    or (result.get('state')=='current' and (set(result)!={'schema','state','workspace_id','package','manifest','declaration','partial','reasons'}
                        or result.get('workspace_id')!=workspace or result.get('package')!=query['query']))):
                result=unavailable_library()
            output=json.dumps(result,ensure_ascii=False,separators=(',',':'))
            wrapper={'output':output,'exit_code':0 if result.get('state')=='current' else 1}
            if len(json.dumps(wrapper,ensure_ascii=False,separators=(',',':')).encode())>32768:
                result=unavailable_library();wrapper={'output':json.dumps(result,separators=(',',':')),'exit_code':1}
            return wrapper
        # One payload, not a second duplicate copy in metadata. Locators/hashes
        # and actual excerpts remain visible for retained-context dedup.
        budgets = query.get("budgets", DEFAULT_BUDGETS)
        def wrapper(value):
            return {"output": json.dumps(value, ensure_ascii=False, separators=(",", ":")), "exit_code": 1 if value.get("error") else 0}
        for attempt in range(2):
            try:
                for _ in range(12):
                    result = measure_envelope(result, budgets)
                    out = wrapper(result)
                    size = len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode())
                    if result["cost"].get("tool_wrapper_bytes") == size:
                        break
                    result["cost"]["tool_wrapper_bytes"] = size
                if size <= min(budgets["bytes"], budgets["tokens"]):
                    return out
            except CodeQueryError:
                pass
            result = finalize_error(query, "budget_exceeded", "Complete tool envelope exceeds budget", observed_cost=result.get("cost"))
        # Failure envelopes contain no source payload or cursor. Keep this
        # compact boundary explicit instead of silently truncating success.
        compact = {k: result[k] for k in ("schema", "operation", "receipt", "error", "partial", "degraded", "cost")}
        compact["cost"] = {k: v for k, v in compact["cost"].items() if v and k in {"files_bytes_read", "validation_bytes_read", "native_rpc_calls_completed"}}
        compact["error"]["message"] = "Tool envelope exceeds budget"
        for _ in range(12):
            compact = measure_envelope(compact, budgets)
            out = wrapper(compact)
            size = len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode())
            if compact["cost"].get("tool_wrapper_bytes") == size:
                return out
            compact["cost"]["tool_wrapper_bytes"] = size
        return out
