"""Code navigation through authenticated native Files authority."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from src import tool_execution
from src.tool_execution import _direct_fallback
from src.openclank.filesystem_registry import FilesystemRootRegistry
from src.openclank.files_service_client import close_all_clients


@pytest.fixture(autouse=True)
def isolated_file_authority(tmp_path, monkeypatch):
    """A JSON transport hint does not isolate the canonical policy authority."""
    from src import constants, secret_storage, tool_security
    from src.openclank import file_policy, filesystem_registry, files_service_client
    from core import auth
    data = tmp_path / "authority"
    data.mkdir()
    paths = {"DATA_DIR": str(data), "APP_DB": str(data / "app.db"),
             "AUTH_FILE": str(data / "auth.json"), "FM_DB_PATH": str(data / "fm.db")}
    for name, value in paths.items():
        monkeypatch.setattr(constants, name, value)
    for name, value in {"OPEN_CLANK_DATA_DIR": paths["DATA_DIR"], "ODYSSEUS_DATA_DIR": paths["DATA_DIR"],
                        "OPEN_CLANK_AUTHORITY_DB_PATH": paths["APP_DB"],
                        "OPEN_CLANK_AUTHORITY_AUTH_PATH": paths["AUTH_FILE"], "FM_DB_PATH": paths["FM_DB_PATH"]}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ODYSSEUS_FILES_TRANSPORT", "framed")
    for name in list(os.environ):
        if name.startswith("OPENCLANK_HISTORY_") or name in {"ODYSSEUS_FILES_SOCKET", "ODYSSEUS_FILES_GRPC_SOCKET", "ODYSSEUS_FILES_GRPC_SESSION", "OPEN_CLANK_AGENT_HOME", "THESIUS_AGENT_HOME"}:
            monkeypatch.delenv(name, raising=False)
    for module in (file_policy, filesystem_registry):
        monkeypatch.setattr(module, "APP_DB", paths["APP_DB"])
    monkeypatch.setattr(auth, "DEFAULT_AUTH_PATH", paths["AUTH_FILE"])
    monkeypatch.setattr(auth.AuthManager.__init__, "__defaults__", (paths["AUTH_FILE"],))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", data / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(files_service_client, "_HISTORY_BINDING_PROVIDER", None)
    assert secret_storage._KEY_PATH.resolve().is_relative_to(tmp_path.resolve())
    assert all(Path(value).resolve().is_relative_to(tmp_path.resolve()) for value in paths.values())
    original_registry_init = filesystem_registry.FilesystemRootRegistry.__init__
    def checked_registry_init(self, *args, **kwargs):
        original_registry_init(self, *args, **kwargs)
        assert Path(self.repository.db_path).resolve().is_relative_to(tmp_path.resolve())
    monkeypatch.setattr(filesystem_registry.FilesystemRootRegistry, "__init__", checked_registry_init)
    original_environment = files_service_client.OdysseusFilesClient._service_environment
    def checked_environment(self):
        environment = original_environment(self)
        for name in ("OPEN_CLANK_DATA_DIR", "ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH",
                     "OPEN_CLANK_AUTHORITY_AUTH_PATH", "FM_DB_PATH", "ODYSSEUS_FILES_REGISTRY"):
            if environment.get(name):
                assert Path(environment[name]).resolve().is_relative_to(tmp_path.resolve()), name
        return environment
    monkeypatch.setattr(files_service_client.OdysseusFilesClient, "_service_environment", checked_environment)
    close_all_clients()
    repository = file_policy.FilePolicyRepository(paths["APP_DB"])
    assert Path(repository.db_path).resolve().is_relative_to(tmp_path.resolve())
    users = {name: {"account_id": f"account-s02a-{name}", "is_admin": name in {"administrator", "owner"}}
             for name in ("administrator", "owner", "alice", "bob", "non-admin")}
    Path(paths["AUTH_FILE"]).write_text(json.dumps({"users": users}), encoding="utf-8")
    repository.sync_subjects(users)
    assert tool_security.owner_is_admin_or_single_user("alice") is False
    assert tool_security.owner_is_admin_or_single_user("bob") is False
    assert tool_security.owner_is_admin_or_single_user("administrator") is True
    yield repository
    close_all_clients()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "sub" / "deep").mkdir(parents=True)
    (root / ".hidden").mkdir()
    for name, text in {"a.py": "import os\n# needle a*b here\nprint('x')\n",
                       "sub/b.txt": "nothing\nNEEDLE upper ab\n",
                       "sub/deep/c.py": "# deep python a*b\n",
                       ".hidden/secret.py": "needle a*b\n",
                       "sub/東京.py": "unicode\n"}.items():
        (root / name).write_text(text, encoding="utf-8")
    registry = FilesystemRootRegistry(tmp_path / "roots.json")
    ceiling = registry.add("administrator", str(root), "recursive_directory", ["read"])
    registry.assign_visibility("administrator", "alice", ceiling["id"], ["read"])
    registry.add("alice", str(root), "recursive_directory", ["read"])
    monkeypatch.setenv("ODYSSEUS_FILES_REGISTRY", str(registry.path))
    monkeypatch.setenv("ODYSSEUS_FILES_SERVICE_BIN", str(Path(__file__).resolve().parents[1] / "packages/odysseus-files/target/debug/odysseus-files-service"))
    token = tool_execution._active_workspace.set(str(root))
    try:
        yield root
    finally:
        tool_execution._active_workspace.reset(token)
        close_all_clients()


def run(tool, repo, **args):
    return asyncio.run(_direct_fallback(tool, json.dumps({"path": str(repo), **args}), owner="alice"))


def test_grep_returns_matching_files_not_line_occurrences(repo):
    result = run("grep", repo, pattern="needle")
    assert result["exit_code"] == 0
    assert [row["path"] for row in result["matches"]] == [str(repo / "a.py")]
    assert result["metadata"]["file"]["page"]["unit"] == "file"


def test_literal_star_is_preserved(repo):
    result = run("grep", repo, pattern="a*b")
    assert {row["path"] for row in result["matches"]} == {str(repo / "a.py"), str(repo / "sub/deep/c.py")}
    assert str(repo / "sub/b.txt") not in result["output"]


def test_ignore_case_and_glob_filter(repo):
    result = run("grep", repo, pattern="needle", ignore_case=True, glob="*.py")
    assert [row["path"] for row in result["matches"]] == [str(repo / "a.py")]
    assert str(repo / "sub/b.txt") in run("grep", repo, pattern="needle", ignore_case=True)["output"]


def test_literal_whitespace_is_preserved(repo):
    assert run("grep", repo, pattern=" needle ")["matches"]
    assert run("grep", repo, pattern=" needle  ")["matches"] == []


@pytest.mark.parametrize("pattern,expected", [
    ("*.py", {"a.py", "sub/deep/c.py", "sub/東京.py"}),
    ("**/*.py", {"a.py", "sub/deep/c.py", "sub/東京.py"}),
    ("sub/*.py", {"sub/東京.py"}),
    ("sub/**/c.py", {"sub/deep/c.py"}),
    ("b.txt", {"sub/b.txt"}),
    ("sub/??.py", {"sub/東京.py"}),
])
def test_real_glob_dialect(repo, pattern, expected):
    result = run("glob", repo, pattern=pattern)
    assert result["exit_code"] == 0
    assert {str(Path(path).relative_to(repo)) for path in result["paths"]} == expected
    assert result["search_mode"] == "glob"


@pytest.mark.parametrize("pattern", ["[bad", "{x,y}", "sub/**x.py", "../*.py"])
def test_invalid_glob_is_error(repo, pattern):
    result = run("glob", repo, pattern=pattern)
    assert result["exit_code"] == 1
    assert result["code"] == "malformed_request"


def test_hidden_is_explicit(repo):
    assert str(repo / ".hidden/secret.py") not in run("glob", repo, pattern="**/*.py")["output"]
    assert str(repo / ".hidden/secret.py") in run("glob", repo, pattern="**/*.py", include_hidden=True)["output"]


def test_bounded_nonresumable_receipt(repo):
    result = run("glob", repo, pattern="**/*.py", limit=1)
    assert result["page"]["returned"] == 1
    assert result["page"]["total"] is None
    assert not result["page"]["resumable"]
    assert not result["page"]["complete"]
    assert result["metadata"]["file"]["page"] == result["page"]
    assert result["continuation"]["supported"] is False
    assert run("glob", repo, pattern="*.py", cursor=7)["code"] == "unsupported"


def test_regex_is_explicitly_unsupported(repo):
    assert run("grep", repo, pattern="a.*b", mode="regex")["code"] == "unsupported"


def test_no_match_is_complete_not_failure(repo):
    result = run("grep", repo, pattern="zzzznotfound")
    assert result["exit_code"] == 0 and result["matches"] == []
    assert result["page"]["complete"] and result["page"]["total"] == 0


@pytest.mark.parametrize("tool", ["grep", "glob"])
def test_pattern_required(repo, tool):
    assert run(tool, repo)["exit_code"] == 1


def test_search_wrong_owner_and_outside_root_denied(repo):
    wrong = asyncio.run(_direct_fallback("grep", json.dumps({"pattern": "needle", "path": str(repo)}), owner="bob"))
    assert wrong["exit_code"] == 1
    outside = run("grep", repo.parent, pattern="needle")
    assert outside["exit_code"] == 1
    assert outside["code"] in {"denied", "outside_root"}


def test_ls_and_read_use_registered_authority(repo):
    listing = run("ls", repo)
    assert listing["exit_code"] == 0 and "a.py" in listing["output"]
    result = run("read_file", repo / "a.py", offset=2, limit=1)
    assert result["output"] == "# needle a*b here\n"


def test_transport_failure_remains_error(repo, monkeypatch):
    from src.agent_tools import filesystem_tools
    async def failed(*args, **kwargs):
        return {"error": "fixture service unavailable", "exit_code": 1, "code": "service_unavailable"}
    monkeypatch.setattr(filesystem_tools, "_agent_request", failed)
    result = run("grep", repo, pattern="needle")
    assert result["exit_code"] == 1 and result["code"] == "service_unavailable"
    assert "No matches" not in result["error"]


def test_empty_partial_result_is_not_complete_no_match(repo, monkeypatch):
    from src.agent_tools import filesystem_tools
    async def partial(*args, **kwargs):
        return {"data": {"matches": [], "complete": False, "work": {"entries_visited": 10000}}}
    monkeypatch.setattr(filesystem_tools, "_agent_request", partial)
    result = run("grep", repo, pattern="unmatched")
    assert result["page"]["total"] is None
    assert result["page"]["complete"] is False
    assert "scanned portion" in result["output"]
    assert "Partial bounded search" in result["output"]
    assert "continuation is unavailable" in result["output"]
