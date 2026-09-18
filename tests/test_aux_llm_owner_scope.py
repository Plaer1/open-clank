from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _src(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_registered_manual_compaction_uses_session_owner_for_managed_utility_route():
    session_src = _src("routes/session_routes.py")

    assert 'owner = getattr(session, "owner", None) or effective_user(request)' in session_src
    assert 'owner=owner or "local-installation"' in session_src
    assert 'purpose="utility"' in session_src
    assert "complete_text(" in session_src
    assert "resolve_endpoint(" not in session_src


def test_task_name_generation_uses_owner_scoped_managed_route():
    src = _src("routes/task_routes.py")

    assert "async def _generate_task_name(prompt: str, owner: Optional[str] = None)" in src
    assert "q = q.filter(DbSession.owner == owner)" in src
    assert "DbSession.provider_model_route_id.isnot(None)" in src
    assert "model_route_id=model_route_id" in src
    assert 'purpose="tasks"' in src
    assert "await _generate_task_name(req.prompt, owner=user)" in src


def test_auto_compaction_utility_endpoint_keeps_chat_owner():
    helper_src = _src("routes/chat_helpers.py")
    compact_src = _src("src/context_compactor.py")

    assert "owner=user" in helper_src
    assert "owner: Optional[str] = None" in compact_src
    assert 'from src.openclank.modality_facade import complete_text' in compact_src
    assert 'purpose="utility"' in compact_src
    assert "owner=completion_owner or \"\"" in compact_src
    assert "root_operation_id=root_operation_id" in compact_src
    assert "llm_call_async" not in compact_src


def test_background_session_sort_uses_owner_managed_utility_route():
    src = _src("src/session_actions.py")

    assert "complete_text(" in src
    assert 'owner=owner or "local-installation"' in src
    assert 'purpose="utility"' in src
    assert "resolve_task_endpoint" not in src


def test_scheduler_task_execution_uses_normalized_managed_routes():
    src = _src("src/task_scheduler.py")

    assert "resolve_task_candidates(" not in src
    assert 'purpose="tasks"' in src
    assert "_resolve_managed_task_route" in src
    assert "ProviderRouteBinding" not in src
    assert "db2.query(ModelEndpoint)" not in src


def test_agent_tool_free_fallbacks_use_managed_completion():
    src = _src("src/agent_loop.py")

    assert src.count("complete_text(") >= 2
    assert 'purpose="utility"' in src
    assert 'purpose="chat"' in src
    assert "from src.llm_core import llm_call_async" not in src


def test_research_routes_use_owner_scoped_managed_bindings():
    src = _src("routes/research/research_routes.py")

    assert "managed_route_summary(" in src
    assert 'purpose="research"' in src
    assert 'operation="chat.complete"' in src
    assert 'owner=owner or getattr(sess, "owner", None) or ""' in src
    assert "resolve_chat_route(" in src
    assert "ModelEndpoint" not in src
    assert "resolve_endpoint_runtime" not in src
    assert "llm_call_async" not in src
