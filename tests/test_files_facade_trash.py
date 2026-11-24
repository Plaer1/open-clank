"""Facade-level Host trash / restore (B2-P4). Plain asserts; fake provider records calls."""
import asyncio
import tempfile
from pathlib import Path

import src.secret_storage as secret_storage
from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContext, ProviderResource
from src.openclank.files_host_provider import HostFilesProvider, _origin

FILE = "/work/docs/a.txt"
ENTRY = {"id": "trash-abc123", "root_id": "app-host", "original_path": FILE,
         "trashed_path": "/work/docs/.odysseus-trash/trash-abc123", "fingerprint": "sha256:" + "a" * 64 + ":5"}
CTX = ProviderContext(owner_subject_id="account-alice", owner_username="alice", policy_generation=4)


def _isolate():
    secret_storage._KEY_PATH = Path(tempfile.mkdtemp()) / ".app_key"
    secret_storage._fernet = None


class FakeHost:
    name = "host"

    def __init__(self, parent_writable=True, outcome=None, name="host"):
        self.name = name
        self.stats, self.actions, self.parent_writable, self.outcome = [], [], parent_writable, outcome

    async def stat(self, context, *, origin_id):
        self.stats.append(origin_id)
        if origin_id == _origin(FILE):
            return ProviderResource(origin_id, "a.txt", "file", ("stat", "trash", "restore"))
        return ProviderResource(origin_id, "docs", "folder", ("children", "stat", *(("write",) if self.parent_writable else ())))

    async def action(self, context, *, origin_id, action, args, action_id=None, expected_revision=None):
        self.actions.append((origin_id, action, args, action_id, expected_revision))
        if self.outcome:
            raise FilesFacadeError("lost", code=self.outcome)
        entry = ProviderResource(origin_id, "a.txt", "file", ("stat", "trash"), action_receipt={
            "action_id": "r1", "status": "complete", "receipt": {"outcome": action, "trash_entry": dict(ENTRY)}})
        return entry


def _setup(**kw):
    _isolate()
    provider = FakeHost(**kw)
    facade = FilesFacade([provider])
    ref = facade._resource(CTX, provider.name, ProviderResource(_origin(FILE), "a.txt", "file", ("stat", "trash", "restore")))["ref"]
    return provider, facade, ref


def _run(facade, ref, action, args, **kw):
    return asyncio.run(facade.action(CTX, resource_ref=ref, action=action, args=args, **kw))


def _refused(facade, ref, action, args):
    try:
        _run(facade, ref, action, args)
    except FilesFacadeError:
        return True
    return False


def test_trash_reaches_provider_once_and_returns_trash_entry():
    provider, facade, ref = _setup()
    result = _run(facade, ref, "trash", {}, action_id="a-1")
    assert len(provider.actions) == 1 and provider.actions[0][:3] == (_origin(FILE), "trash", {})
    assert result["history"]["receipt"]["trash_entry"] == ENTRY and result["state"] == {"trashed": True}


def test_trash_with_args_refused_before_provider():
    provider, facade, ref = _setup()
    assert _refused(facade, ref, "trash", {"entry": ENTRY}) and _refused(facade, ref, "trash", {"x": 1})
    assert not provider.actions


def test_host_restore_reaches_provider_unchanged_without_statting_origin():
    provider, facade, ref = _setup()
    result = _run(facade, ref, "restore", {"entry": dict(ENTRY)}, action_id="u-1")
    assert provider.actions == [(_origin(FILE), "restore", {"entry": ENTRY}, "u-1", None)]
    assert _origin(FILE) not in provider.stats and provider.stats == [_origin("/work/docs")]
    assert result["history"]["receipt"]["trash_entry"] == ENTRY and result["state"] == {"trashed": False}


def test_malformed_restore_entries_refused_before_provider():
    provider, facade, ref = _setup()
    bad = [{"entry": "x"}, {"entry": {k: v for k, v in ENTRY.items() if k != "id"}},
           {"entry": {**ENTRY, "extra": "x"}}, {"entry": {**ENTRY, "id": 5}}, {"entry": {**ENTRY, "original_path": "/" + "a" * 5000}},
           {"entry": {**ENTRY, "id": ""}}, {"entry": dict(ENTRY), "other": 1}, {"entry": {**ENTRY, "original_path": "/work/other.txt"}},
           {"entry": {**ENTRY, "original_path": "relative.txt"}}]
    for args in bad:
        assert _refused(facade, ref, "restore", args), args
    assert not provider.actions


def test_restore_refused_when_parent_not_writable():
    provider, facade, ref = _setup(parent_writable=False)
    assert _refused(facade, ref, "restore", {"entry": dict(ENTRY)}) and not provider.actions


def test_restore_args_on_non_host_provider_refused_and_plain_restore_unchanged():
    provider, facade, _ = _setup(name="files")
    ref = facade._resource(CTX, "files", ProviderResource(_origin(FILE), "a.txt", "file", ("stat", "trash", "restore")))["ref"]
    assert _refused(facade, ref, "restore", {"entry": dict(ENTRY)}) and not provider.actions
    _run(facade, ref, "restore", {})
    assert len(provider.actions) == 1 and provider.actions[0][1:3] == ("restore", {})


def test_unknown_outcome_passes_through_with_one_provider_call():
    for action, args in (("trash", {}), ("restore", {"entry": dict(ENTRY)})):
        provider, facade, ref = _setup(outcome="mutation_outcome_unknown")
        try:
            _run(facade, ref, action, args)
            assert False
        except FilesFacadeError as error:
            assert error.code == "mutation_outcome_unknown"
        assert len(provider.actions) == 1


def test_caps_trash_without_restore_for_writable_regular_file_only():
    caps = HostFilesProvider._caps("file", writable=True)
    assert "trash" in caps and "restore" not in caps
    assert "trash" not in HostFilesProvider._caps("file", writable=False)
    for kind in ("folder", "directory", "recursive_directory"):
        folder = HostFilesProvider._caps(kind, writable=True)
        assert "trash" not in folder and "restore" not in folder
    saved = HostFilesProvider._LOOSE_MANAGED_MOVE_READY
    HostFilesProvider._LOOSE_MANAGED_MOVE_READY = False
    try:
        unsupported = HostFilesProvider._caps("file", writable=True)
    finally:
        HostFilesProvider._LOOSE_MANAGED_MOVE_READY = saved
    assert "trash" not in unsupported and "restore" not in unsupported


def test_route_request_model_is_closed():
    from routes.files_facade_routes import TrashEntryRequest
    TrashEntryRequest.model_validate(ENTRY)
    for bad in ({**ENTRY, "extra": "x"}, {k: v for k, v in ENTRY.items() if k != "id"}, {**ENTRY, "id": 1}, {**ENTRY, "original_path": "a" * 4097}):
        try:
            TrashEntryRequest.model_validate(bad)
            assert False, bad
        except ValueError:
            pass
