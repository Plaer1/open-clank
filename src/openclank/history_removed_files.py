"""Removed files of ONE open Host folder, read from History's own registry (no store, no filesystem scan).

History's registered-resource list already includes inactive registrations (a trashed file's registration goes
inactive). A registration qualifies only when it belongs to this account, sits directly in the folder the caller
named, and its leaf is really absent; the folder itself must be a current, writable Host folder.
"""
from __future__ import annotations
import os
from pathlib import Path

from src.openclank.files_facade import FilesFacadeError
from src.openclank.files_host_provider import host_origin_for_path
from src.openclank.history_client import HistoryAvailabilityError, registered_resource_pages
from src.openclank.history_io import bounded_history_io
from src.openclank.resource_refs import stable_resource_id

MAX_CANDIDATES = 200
_DIRS = {"folder", "directory", "recursive_directory"}


async def removed_files(client, *, account, context, provider, folder_path, cursor=0, limit=25):
    folder = os.path.normpath(str(folder_path))
    if not os.path.isabs(folder) or os.path.realpath(folder) != folder:
        raise HistoryAvailabilityError("resource_not_found", "folder unavailable")
    try:
        entry = await provider.resource_for_path(context, path=folder)
    except FilesFacadeError as exc:
        raise HistoryAvailabilityError("resource_not_found", "folder unavailable") from exc
    if entry.kind not in _DIRS or not {"children", "write"}.issubset(set(entry.capabilities)):
        raise HistoryAvailabilityError("resource_not_found", "folder unavailable")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or not 1 <= int(limit) <= 50:
        raise HistoryAvailabilityError("invalid_cursor", "page unavailable")

    async def fetch_page(token):
        return await bounded_history_io(lambda: client.list_registered_resources(cursor=token, limit=100), timeout=1.5)

    found = {}
    async for page in registered_resource_pages(fetch_page):
        for row in page["items"]:
            if row.get("account_id") != account or not row.get("workspace_id"):
                continue
            root, relative = str(row.get("root_path") or ""), str(row.get("relative_path") or "")
            rel = Path(relative)
            if not root or not os.path.isabs(root) or not relative or rel.is_absolute() or ".." in rel.parts:
                continue
            path = os.path.join(root, relative)
            if os.path.dirname(path) != folder or os.path.lexists(path):
                continue
            found.setdefault(path, []).append(str(row.get("resource_id") or ""))
        if len(found) >= MAX_CANDIDATES:
            break
    names = sorted(found)[:MAX_CANDIDATES]
    items = []
    for path in names[cursor:cursor + int(limit)]:
        best = None
        for private in found[path]:
            if not private.startswith("file:"):
                continue
            versions = (await bounded_history_io(lambda p=private: client.list_resource_versions(p, limit=20), timeout=1.5))["items"]
            before = [v for v in versions if v.get("availability") == "available" and v.get("phase") == "before"]
            gone = [v for v in versions if v.get("availability") == "absent" and v.get("phase") == "after"]
            if not before:
                continue
            removed = (gone[0] if gone else before[0]).get("timestamp_millis") or 0
            if best is None or removed > best["removed_at_ms"]:
                best = {"removed_at_ms": removed, "size_bytes": before[0].get("size_bytes")}
        if best is None:
            continue
        public = stable_resource_id(owner_subject_id=account, provider="host", origin_id=host_origin_for_path(path))
        items.append({"name": os.path.basename(path), "removed_at_ms": best["removed_at_ms"], "size_bytes": best["size_bytes"], "resource_id": public})
    more = cursor + int(limit) < len(names)
    return {"version": 1, "scope": "one_open_folder", "items": items,
            "next_cursor": cursor + int(limit) if more else None, "coverage": "registered_history_only"}


async def folder_changes(client, *, account, context, provider, folder_path, cursor=0, limit=25):
    """Files of ONE open folder that History recorded: added, changed or removed, with time and the changing action.

    Same authority and bounds as removed_files (direct children, same account, writable non-symlink folder); present
    files are included. Ordered by name (stable paging); at most 200 candidates, at most one page of version reads."""
    folder = os.path.normpath(str(folder_path))
    if not os.path.isabs(folder) or os.path.realpath(folder) != folder:
        raise HistoryAvailabilityError("resource_not_found", "folder unavailable")
    try:
        entry = await provider.resource_for_path(context, path=folder)
    except FilesFacadeError as exc:
        raise HistoryAvailabilityError("resource_not_found", "folder unavailable") from exc
    if entry.kind not in _DIRS or not {"children", "write"}.issubset(set(entry.capabilities)):
        raise HistoryAvailabilityError("resource_not_found", "folder unavailable")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0 or not 1 <= int(limit) <= 50:
        raise HistoryAvailabilityError("invalid_cursor", "page unavailable")

    async def fetch_page(token):
        return await bounded_history_io(lambda: client.list_registered_resources(cursor=token, limit=100), timeout=1.5)

    found = {}
    async for page in registered_resource_pages(fetch_page):
        for row in page["items"]:
            if row.get("account_id") != account or not row.get("workspace_id"):
                continue
            root, relative = str(row.get("root_path") or ""), str(row.get("relative_path") or "")
            rel = Path(relative)
            if not root or not os.path.isabs(root) or not relative or rel.is_absolute() or ".." in rel.parts:
                continue
            path = os.path.join(root, relative)
            if os.path.dirname(path) == folder:
                found.setdefault(path, []).append(str(row.get("resource_id") or ""))
        if len(found) >= MAX_CANDIDATES:
            break
    names = sorted(found)[:MAX_CANDIDATES]
    items = []
    for path in names[cursor:cursor + int(limit)]:
        versions = []
        for private in found[path]:
            if private.startswith("file:"):
                versions += (await bounded_history_io(lambda p=private: client.list_resource_versions(p, limit=20), timeout=1.5))["items"]
        stamped = [v for v in versions if isinstance(v.get("timestamp_millis"), int)]
        if not stamped:
            continue
        newest = max(stamped, key=lambda v: v["timestamp_millis"])
        present = os.path.lexists(path)
        state = "removed" if not present else "added" if any(v.get("phase") == "before" and v.get("availability") == "absent" for v in stamped) else "changed"
        sizes = [v.get("size_bytes") for v in stamped if v.get("availability") == "available" and isinstance(v.get("size_bytes"), int)]
        public = stable_resource_id(owner_subject_id=account, provider="host", origin_id=host_origin_for_path(path))
        items.append({"name": os.path.basename(path), "state": state, "at_ms": newest["timestamp_millis"], "action": str(newest.get("operation") or "")[:64],
                      "action_id": str(newest.get("action_id") or "")[:128], "size_bytes": sizes[-1] if sizes else None, "resource_id": public})
    more = cursor + int(limit) < len(names)
    return {"version": 1, "scope": "one_open_folder", "items": items, "next_cursor": cursor + int(limit) if more else None,
            "coverage": "registered_history_only"}
