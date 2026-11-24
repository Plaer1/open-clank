"""BQ-16: a trashed/deleted original resolves to its History registration only on request, only with an authorized parent."""
import asyncio
import os
import tempfile
from types import SimpleNamespace

from src.openclank.files_facade import FilesFacadeError
from src.openclank.files_host_provider import host_origin_for_path
from src.openclank.history_change_qualifier import resolve_registered_history_ids
from src.openclank.history_client import HistoryAvailabilityError
from src.openclank.resource_refs import stable_resource_id

ACCOUNT = "account-alice"


class Client:
    def __init__(self, root, relative, active=False):
        self.row = {"account_id": ACCOUNT, "workspace_id": "files:r", "root_path": root, "relative_path": relative,
                    "resource_id": "file:registry:abc", "active": active}

    def list_registered_resources(self, cursor=None, limit=100):
        return {"items": [self.row], "next_cursor": None}


class Provider:
    """Stats only paths in `present`; a folder is writable when it is listed in `writable`."""
    def __init__(self, present, writable):
        self.present, self.writable = set(present), set(writable)

    async def resource_for_path(self, context, *, path):
        if path not in self.present:
            raise FilesFacadeError("gone", code="resource_unavailable")
        caps = ("children", "write") if path in self.writable else ("stat",)
        return SimpleNamespace(origin_id=host_origin_for_path(path), capabilities=caps)


def resolve(root, name, provider, **kw):
    path = os.path.join(root, name)
    public = stable_resource_id(owner_subject_id=ACCOUNT, provider="host", origin_id=host_origin_for_path(path))
    found = {}
    result = asyncio.run(resolve_registered_history_ids(Client(root, name), public, account=ACCOUNT, context=None, provider=provider,
                                                         resolved_paths=found, **kw))
    return result, found, path


def refused(root, name, provider, **kw):
    try:
        resolve(root, name, provider, **kw)
    except HistoryAvailabilityError as exc:
        assert exc.code == "resource_not_found"
        return True
    return False


def test_absent_original_resolves_only_when_asked_and_the_parent_is_authorized():
    root = os.path.realpath(tempfile.mkdtemp())
    gone = Provider(present={root}, writable={root})
    assert refused(root, "a.txt", gone), "default resolver must keep refusing an absent original"
    result, found, path = resolve(root, "a.txt", gone, allow_absent=True)
    assert result == ["file:registry:abc"] and found == {"file:registry:abc": path}
    # parent not writable / not authorized: still refused
    assert refused(root, "a.txt", Provider(present={root}, writable=set()), allow_absent=True)
    assert refused(root, "a.txt", Provider(present=set(), writable=set()), allow_absent=True)


def test_a_leaf_that_exists_but_is_not_authorized_is_never_resolved_by_the_absent_rule():
    root = os.path.realpath(tempfile.mkdtemp())
    open(os.path.join(root, "a.txt"), "w").close()
    assert refused(root, "a.txt", Provider(present={root}, writable={root}), allow_absent=True)


def test_present_original_resolves_as_before():
    root = os.path.realpath(tempfile.mkdtemp())
    path = os.path.join(root, "a.txt")
    result, _found, _p = resolve(root, "a.txt", Provider(present={root, path}, writable={root}))
    assert result == ["file:registry:abc"]
