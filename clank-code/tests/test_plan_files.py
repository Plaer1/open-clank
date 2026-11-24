"""Plan files on disk — canonical owner .clanker/futures/ convention.

Covers slug derivation, the approval-boundary materialization write, path
containment, and the update_plan on-disk mirror while executing.
"""

import asyncio
from pathlib import Path

from src.plan_files import FUTURES_DIR, materialize_plan, metaplan_relpath, plan_slug


def test_slug_from_heading():
    assert plan_slug("# QOL Pass 2026\n\n- [ ] step") == "qol-pass-2026"


def test_slug_from_checklist_item():
    assert plan_slug("- [ ] Fix the Permissions panel\n- [ ] next") == "fix-the-permissions-panel"


def test_slug_fallback_and_sanitization():
    assert plan_slug("") == "plan"
    assert plan_slug("!!!") == "plan"
    # Long titles are capped.
    assert len(plan_slug("# " + "word " * 40)) <= 60


def test_metaplan_relpath_layout():
    assert metaplan_relpath("# My Plan") == f"{FUTURES_DIR}/my-plan.md"


def test_materialize_plan_writes_metaplan(tmp_path):
    plan = "# Build the thing\n\n- [ ] step one\n"
    rel = materialize_plan(str(tmp_path), plan)

    assert rel == f"{FUTURES_DIR}/build-the-thing.md"
    written = (tmp_path / rel).read_text(encoding="utf-8")
    assert written == plan.strip() + "\n"


def test_materialize_plan_empty_inputs(tmp_path):
    assert materialize_plan("", "# Plan") is None
    assert materialize_plan(str(tmp_path), "") is None
    assert materialize_plan(str(tmp_path), "   ") is None
    assert materialize_plan(str(tmp_path / "missing"), "# Plan") is None


def test_materialize_plan_revises_same_file(tmp_path):
    first = "# Staged work\n\n- [ ] stage one\n"
    revised = "# Staged work\n\n- [x] stage one\n"
    rel1 = materialize_plan(str(tmp_path), first)
    rel2 = materialize_plan(str(tmp_path), revised)

    assert rel1 == rel2
    assert "- [x] stage one" in (tmp_path / rel2).read_text(encoding="utf-8")


def test_materialize_plan_keeps_explicit_artifact_path_across_heading_changes(tmp_path):
    first = "# First heading\n- [ ] step\n"
    revised = "# A different heading\n- [x] step\n"
    legacy = ".futures/stable-plan.md"
    rel = ".clanker/futures/stable-plan.md"
    assert materialize_plan(str(tmp_path), first, relative_path=legacy) == rel
    assert materialize_plan(str(tmp_path), revised, relative_path=rel) == rel
    assert (tmp_path / rel).read_text(encoding="utf-8").startswith("# A different")


def test_materialize_plan_rejects_escape_path(tmp_path):
    assert materialize_plan(str(tmp_path), "# no", relative_path=".futures/../outside.md") is None
    assert not (tmp_path / "outside.md").exists()


def test_update_plan_mirrors_to_workspace(tmp_path, monkeypatch):
    from src.agent_tools.interaction_tools import UpdatePlanTool
    from src.tool_execution import _active_workspace

    token = _active_workspace.set(str(tmp_path))
    try:
        desc, result = asyncio.run(
            UpdatePlanTool().execute('{"plan": "# Ship it\\n- [x] done\\n- [ ] next"}', None)
        )
    finally:
        _active_workspace.reset(token)

    assert result["exit_code"] == 0
    rel = f"{FUTURES_DIR}/ship-it.md"
    assert rel in result["output"]
    assert "- [x] done" in (tmp_path / rel).read_text(encoding="utf-8")


def test_update_plan_without_workspace_still_works():
    from src.agent_tools.interaction_tools import UpdatePlanTool

    # No active workspace contextvar: pure UI behavior, no disk write.
    desc, result = asyncio.run(UpdatePlanTool().execute('{"plan": "- [ ] only step"}', None))

    assert result["exit_code"] == 0
    assert "plan_update" in result
    assert "Saved to" not in result["output"]
