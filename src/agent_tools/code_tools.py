"""Friendly read-only code tool; existing app/broker owns all authority."""
import json
import os


class SearchCodeTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.openclank.code_query import CodeQueryError, finalize_error, query_code, validate_query, measure_envelope, DEFAULT_BUDGETS
        from src.memory_provider import MemoryRequestRejectedError
        from src import ai_interaction
        owner = str(ctx.get("owner") or "")
        sid = str(ctx.get("session_id") or "")
        workspace = str(ctx.get("authority_workspace_id") or "")
        query = {}
        try:
            query = validate_query(json.loads(content))
        except (TypeError, ValueError, CodeQueryError):
            result = finalize_error({}, "invalid", "search_code requires a bounded code-query JSON object")
        else:
            provider = getattr(ai_interaction, "_memory_provider", None)
            try:
                if not owner or not sid or not workspace or provider is None:
                    raise CodeQueryError("denied", "Code chat workspace unavailable")
                if getattr(provider, "_broker_url", ""):
                    result = await provider.code_query_session(query=query, session_id=sid, authority_workspace_id=workspace)
                else:
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
