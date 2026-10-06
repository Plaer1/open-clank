import pytest

from routes.task_routes import TaskCreate, TaskUpdate
from core.database import ScheduledTask


def test_copal_workspace_is_independent_logical_task_field():
    create = TaskCreate(prompt="read my Copal timeline")
    update = TaskUpdate(copal_workspace="school")
    assert create.copal_workspace == "default"
    assert update.copal_workspace == "school"
    assert hasattr(ScheduledTask, "copal_workspace")
    assert ScheduledTask.copal_workspace.default.arg == "default"


def test_default_assistant_copal_migration_preserves_custom_tool_lists():
    from src.task_scheduler import (
        COPAL_DEFAULT_ASSISTANT_TOOLS,
        HISTORICAL_DEFAULT_ASSISTANT_TOOLS,
        migrate_copal_default_tools,
    )

    historical = list(HISTORICAL_DEFAULT_ASSISTANT_TOOLS)
    assert migrate_copal_default_tools(historical) == historical + list(COPAL_DEFAULT_ASSISTANT_TOOLS)
    customized = historical[:-1] + ["custom_tool", historical[-1]]
    assert migrate_copal_default_tools(customized) == customized
    assert migrate_copal_default_tools({"tools": historical}) == {"tools": historical}


def test_acp_lifetools_descriptor_carries_pinned_copal_workspace(monkeypatch, tmp_path):
    from src.openclank.acp_bridge import lifetools_mcp_descriptor

    monkeypatch.setenv("COPAL_LOOSE_ROOT", str(tmp_path / "copal-vaults"))
    descriptor = lifetools_mcp_descriptor(owner="alice", session_id="task-1", copal_workspace="school")
    env = {item["name"]: item["value"] for item in descriptor["env"]}
    assert env["COPAL_WORKSPACE"] == "school"
    assert env["COPAL_LOOSE_ROOT"] == str(tmp_path / "copal-vaults")


@pytest.mark.asyncio
async def test_native_tool_door_rejects_copal_workspace_drift():
    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block

    description, result = await execute_tool_block(
        ToolBlock(tool_type="read_copal", content='{"action":"notes.list","workspace":"other"}'),
        owner="alice",
        copal_workspace="school",
    )
    assert description == "read_copal: BLOCKED"
    assert result["code"] == "copal_workspace_mismatch"
    assert result["expected"] == "school"
