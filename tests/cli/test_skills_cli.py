import sys
import types
from unittest.mock import MagicMock

import pytest

from tests.helpers.cli_loader import load_script


@pytest.fixture
def cli(monkeypatch):
    mod = types.ModuleType("services.memory.skills")
    mod.SkillsManager = MagicMock()
    monkeypatch.setitem(sys.modules, "services.memory.skills", mod)
    return load_script("odysseus-skills")


def test_preview_text_ignores_non_string(cli):
    assert cli._preview_text(None) == ""
    assert cli._preview_text(123) == ""
    assert cli._preview_text({"x": 1}) == ""
    assert cli._preview_text("y" * 250) == "y" * 200


def test_summary_does_not_crash_on_non_string_description(cli):
    out = cli._summary({"name": "n", "description": 123})
    assert out["description"] == ""


def test_skill_entries_skips_invalid_rows(cli):
    assert cli._skill_entries([
        {"name": "deploy", "category": "ops"},
        "bad-row",
        None,
    ]) == [{"name": "deploy", "category": "ops"}]
