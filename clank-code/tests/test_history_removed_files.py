"""BQ-16 packet N: removed files of one open folder, from History's registry only."""
import asyncio
import os
import tempfile
from types import SimpleNamespace

from src.openclank.files_facade import FilesFacadeError
from src.openclank.files_host_provider import host_origin_for_path
from src.openclank.history_client import HistoryAvailabilityError
from src.openclank.history_removed_files import removed_files

ACCOUNT = "account-alice"


def row(root, relative, rid, account=ACCOUNT, active=False):
    return {"account_id": account, "workspace_id": "files:r", "root_path": root, "relative_path": relative, "resource_id": rid, "active": active}


class Client:
    def __init__(self, rows, versions):
        self.rows, self.versions, self.version_calls = rows, versions, []

    def list_registered_resources(self, cursor=None, limit=100):
        return {"items": self.rows, "next_cursor": None}

    def list_resource_versions(self, rid, limit=50, cursor=None):
        self.version_calls.append(rid)
        return {"items": self.versions.get(rid, []), "next_cursor": None}


class Provider:
    def __init__(self, folder, caps=("children", "write"), kind="folder"):
        self.folder, self.caps, self.kind = folder, caps, kind

    async def resource_for_path(self, context, *, path):
        if path != self.folder:
            raise FilesFacadeError("no", code="resource_unavailable")
        return SimpleNamespace(origin_id=host_origin_for_path(path), kind=self.kind, capabilities=self.caps)


def pair(size, removed):
    return [{"availability": "absent", "phase": "after", "timestamp_millis": removed, "size_bytes": 0},
            {"availability": "available", "phase": "before", "timestamp_millis": 1, "size_bytes": size}]


def run(client, provider, folder, **kw):
    return asyncio.run(removed_files(client, account=ACCOUNT, context=None, provider=provider, folder_path=folder, **kw))


def folder():
    return os.path.realpath(tempfile.mkdtemp())


def test_lists_only_absent_direct_children_of_the_open_folder_with_name_time_size():
    f = folder(); sub = os.path.join(f, "sub"); os.mkdir(sub)
    open(os.path.join(f, "present.txt"), "w").close()
    client = Client([row(f, "gone.txt", "file:registry:1"), row(f, "present.txt", "file:registry:2"),
                     row(f, "sub/deeper.txt", "file:registry:3"), row(f, "bob.txt", "file:registry:4", account="account-bob"),
                     row(os.path.dirname(f), os.path.basename(f) + "-other/x.txt", "file:registry:5")],
                    {"file:registry:1": pair(42, 999)})
    out = run(client, Provider(f), f)
    assert [(i["name"], i["removed_at_ms"], i["size_bytes"]) for i in out["items"]] == [("gone.txt", 999, 42)]
    assert set(out["items"][0]) == {"name", "removed_at_ms", "size_bytes", "resource_id"} and out["items"][0]["resource_id"].startswith("resource-")
    assert client.version_calls == ["file:registry:1"], "no history read for entries outside the folder or other owners"


def test_the_folder_must_be_a_current_writable_host_folder():
    f = folder(); client = Client([row(f, "gone.txt", "file:registry:1")], {"file:registry:1": pair(1, 2)})
    for provider in (Provider(f, caps=("children",)), Provider(f, kind="file"), Provider(os.path.join(f, "elsewhere"))):
        try:
            run(client, provider, f)
        except HistoryAvailabilityError as exc:
            assert exc.code == "resource_not_found"
        else:
            raise AssertionError("listed for an unauthorized folder")
    link = os.path.join(folder(), "link"); os.symlink(f, link)
    try:
        run(client, Provider(link), link)
    except HistoryAvailabilityError:
        pass
    else:
        raise AssertionError("symlinked folder accepted")


def test_pages_are_bounded_and_resume_by_cursor():
    f = folder(); rows = [row(f, f"f{i:02}.txt", f"file:registry:{i}") for i in range(7)]
    client = Client(rows, {f"file:registry:{i}": pair(i, 10 + i) for i in range(7)})
    first = run(client, Provider(f), f, limit=3)
    assert len(first["items"]) == 3 and first["next_cursor"] == 3
    last = run(client, Provider(f), f, limit=3, cursor=6)
    assert [i["name"] for i in last["items"]] == ["f06.txt"] and last["next_cursor"] is None
