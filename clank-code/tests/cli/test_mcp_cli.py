from types import SimpleNamespace

import pytest

from tests.helpers.cli_loader import load_script
from tests.helpers.db_stubs import make_core_db_stub


def _srv(env):
    return SimpleNamespace(id="s1", name="n", transport="stdio", command="c", args="[]",
                           env=env, url=None, is_enabled=1, oauth_config=None, created_at=None)


@pytest.fixture
def cli(monkeypatch):
    make_core_db_stub(monkeypatch, models=["McpServer"])
    return load_script("odysseus-mcp")


def test_serialize_handles_list_env(cli):
    out = cli._serialize(_srv("[1, 2]"))  # JSON array, not object
    assert out["id"] == "s1"


def test_serialize_redacts_dict_env(cli):
    out = cli._serialize(_srv('{"API_KEY": "secret"}'))
    assert out["env"] == {"API_KEY": "***"}


def test_mcp_json_helpers_reject_wrong_shapes(cli):
    assert cli._json_list('["a"]') == ["a"]
    assert cli._json_list('{"not":"list"}') == []
    assert cli._json_list("{bad") == []
    assert cli._json_dict('{"A":"B"}') == {"A": "B"}
    assert cli._json_dict('["bad"]') == {}
    assert cli._json_dict("{bad") == {}
