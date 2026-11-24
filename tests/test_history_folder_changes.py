"""D4: the Lore whole-folder change list (added/changed/removed with time and action), from History's registry only."""
import asyncio
import os

from src.openclank.history_client import HistoryAvailabilityError
from src.openclank.history_removed_files import folder_changes
from tests.test_history_removed_files import ACCOUNT, Client, Provider, folder, row


def v(phase, availability, ts, size=0, op="managed_move", action="act-1"):
    return {"phase": phase, "availability": availability, "timestamp_millis": ts, "size_bytes": size, "operation": op, "action_id": action}


def run(client, provider, f, **kw):
    return asyncio.run(folder_changes(client, account=ACCOUNT, context=None, provider=provider, folder_path=f, **kw))


def test_lists_added_changed_and_removed_with_time_action_and_owner_isolation():
    f = folder()
    for name in ("added.txt", "changed.txt"): open(os.path.join(f, name), "w").close()
    client = Client(
        [row(f, "added.txt", "file:registry:1"), row(f, "changed.txt", "file:registry:2"), row(f, "gone.txt", "file:registry:3"),
         row(f, "bob.txt", "file:registry:4", account="account-bob"), row(f, "sub/deeper.txt", "file:registry:5")],
        {"file:registry:1": [v("before", "absent", 10), v("after", "available", 11, 5, "replace", "a-add")],
         "file:registry:2": [v("before", "available", 20, 3), v("after", "available", 21, 4, "replace", "a-chg")],
         "file:registry:3": [v("before", "available", 30, 9), v("after", "absent", 31, 0, "trash", "a-rm")],
         "file:registry:4": [v("before", "available", 1, 1)]})
    out = run(client, Provider(f), f)
    got = {i["name"]: (i["state"], i["at_ms"], i["action"], i["action_id"]) for i in out["items"]}
    assert got == {"added.txt": ("added", 11, "replace", "a-add"), "changed.txt": ("changed", 21, "replace", "a-chg"),
                   "gone.txt": ("removed", 31, "trash", "a-rm")}, got
    assert "file:registry:4" not in client.version_calls and "file:registry:5" not in client.version_calls
    assert all(set(i) == {"name", "state", "at_ms", "action", "action_id", "size_bytes", "resource_id"} for i in out["items"])


def test_bounds_paging_and_folder_authority():
    f = folder(); rows = [row(f, f"f{i:02}.txt", f"file:registry:{i}") for i in range(7)]
    client = Client(rows, {f"file:registry:{i}": [v("before", "available", i + 1, i)] for i in range(7)})
    first = run(client, Provider(f), f, limit=3)
    assert len(first["items"]) == 3 and first["next_cursor"] == 3 and len(client.version_calls) == 3, "reads only the page"
    for bad in (Provider(f, caps=("children",)), Provider(f, kind="file")):
        try: run(client, bad, f)
        except HistoryAvailabilityError as exc: assert exc.code == "resource_not_found"
        else: raise AssertionError("listed for an unauthorized folder")
    try: run(client, Provider(f), f, limit=51)
    except HistoryAvailabilityError: pass
    else: raise AssertionError("unbounded page accepted")
