"""Desktop-control `open_resource` wiring: the Host callable set on application state at route setup.

It accepts only an opaque Files resource token that belongs to the calling owner, and hands it to the existing
FilesFacade.open_on_host authority; nothing else is opened and no check is bypassed.
"""
import asyncio
from types import SimpleNamespace

import pytest

from routes import files_facade_routes as routes
from src.openclank.file_policy import FilePolicyRepository
from src.openclank.files_facade import FilesFacade
from src.openclank.resource_refs import issue_resource_ref


class Auth:
    ids = {"alice": "acct-alice", "bob": "acct-bob"}

    def account_id(self, owner):
        return self.ids.get(owner)

    def username_for_account_id(self, account):
        return next((name for name, value in self.ids.items() if value == account), None)

    def is_admin(self, owner):
        return False


@pytest.fixture()
def wired(tmp_path):
    state = SimpleNamespace(auth_manager=Auth())
    repository = FilePolicyRepository(tmp_path / "policy.sqlite3")
    routes.setup_files_facade_routes(application_state=state, policy_repository=repository)
    return state, repository


def token_for(repository, account, **kw):
    return issue_resource_ref(owner_subject_id=account, provider="host", origin_id="origin-1", kind="file", capabilities=["open"],
                              policy_generation=repository.generation(), **kw).token


def run(state, owner, ref, app="editor"):
    return asyncio.run(state.agent_open_on_host(owner, ref, app))


def test_valid_token_calls_the_facade_once(wired, monkeypatch):
    state, repository = wired
    calls = []

    async def fake_open(self, context, *, resource_ref, app_id):
        calls.append((context.owner_subject_id, context.owner_username, resource_ref, app_id))
        return {"application": {}}

    monkeypatch.setattr(FilesFacade, "open_on_host", fake_open)
    token = token_for(repository, "acct-alice")
    assert run(state, "alice", token) == {"ok": True}
    assert calls == [("acct-alice", "alice", token, "editor")]


def test_another_owners_token_is_refused_by_the_facade(wired):
    state, repository = wired
    result = run(state, "bob", token_for(repository, "acct-alice"))
    assert result["ok"] is False and result["code"]


@pytest.mark.parametrize("raw", ["/etc/passwd", "../secret", "C:\\Windows\\win.ini", "file:///etc/hosts", "origin-1"])
def test_a_raw_path_is_refused_by_the_real_facade(wired, raw):
    state, _ = wired
    result = run(state, "alice", raw)
    assert result["ok"] is False and result["code"] not in ("", "files_open_unavailable")


def test_unknown_owner_and_bad_types_are_refused(wired):
    state, repository = wired
    token = token_for(repository, "acct-alice")
    assert run(state, "mallory", token) == {"ok": False, "code": "files_open_unavailable"}
    assert run(state, "alice", 5)["ok"] is False
    assert run(state, "alice", token, app=None)["ok"] is False
