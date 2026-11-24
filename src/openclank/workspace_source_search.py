"""Qualified literal matches from one named-human Files Workspace."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from src.openclank.code_query import _files_client
from src.openclank.file_policy import FilePolicyError
from src.openclank.files_facade import FilesFacadeError
from src.openclank.files_host_provider import _origin
from src.openclank.files_service_client import FilesServiceError
from src.openclank.resource_refs import ResourceRefError, issue_resource_ref
from src.openclank.workspace_policy_service import resolve_owned_workspace, WorkspacePolicyServiceError


_RESULTS = 12
_FILE_BYTES = 256 * 1024
_PARTIAL_REASONS = frozenset({
    "timeout", "result_limit", "depth_limit", "entry_limit", "file_byte_limit",
    "descendant_unavailable", "file_changed",
})


def _unavailable(code: str = "resource_unavailable") -> FilesFacadeError:
    return FilesFacadeError("Workspace source search is unavailable", code=code)


async def search_workspace_contents(*, repository, auth_manager, account: str, owner: str,
                                    workspace_id: str, expected_revision: int, query: str,
                                    current) -> dict:
    """Search one resolved app folder with the existing read-only Files client."""
    try:
        needle = query.encode("utf-8")
    except UnicodeError:
        raise _unavailable("invalid_resource_request") from None
    if not query.strip() or len(needle) > 1024 or any(c in query for c in ("\x00", "\r", "\n")):
        raise _unavailable("invalid_resource_request")

    def binding():
        if not current():
            raise _unavailable("resource_changed")
        generation = repository.generation()
        bound = resolve_owned_workspace(repository, workspace_id=workspace_id,
            owner_username=owner, auth_manager=auth_manager, purpose="app_folder", observation_only=True)
        workspace = bound.workspace
        location = repository.get_location(workspace.location_id)
        root = Path(bound.path)
        metadata = root.stat()
        if (workspace.id != workspace_id or workspace.owner_subject_id != account
                or workspace.revision != expected_revision or root.resolve(strict=True) != root
                or repository.generation() != generation):
            raise _unavailable("resource_changed")
        return workspace, location, root, generation, (metadata.st_dev, metadata.st_ino)

    try:
        captured = binding()
        workspace, location, root, generation, _ = captured
        client = _files_client(binding={
            "owner": owner, "workspace_id": workspace.id, "location_id": location.id,
            "root": str(root), "policy_generation": generation,
        }, repository=repository, auth_manager=auth_manager, session=None, purpose="app_folder")
        response = await asyncio.wait_for(client.request("content_search", str(root), {
            "query": query, "mode": "literal", "case_sensitive": True,
            "include_hidden": False, "locations": True, "deadline_ms": 1500,
            "max_results": _RESULTS, "max_entries": 128, "max_depth": 8,
            "max_bytes_per_file": _FILE_BYTES,
        }), timeout=3)
        if binding() != captured:
            raise _unavailable("resource_changed")
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict) or type(data.get("complete")) is not bool:
            raise _unavailable("provider_unavailable")
        matches, locations = data.get("matches"), data.get("locations", [])
        if (not isinstance(matches, list) or len(matches) > _RESULTS
                or any(not isinstance(path, str) or not path for path in matches)
                or not isinstance(locations, list) or len(locations) > _RESULTS):
            raise _unavailable("provider_unavailable")

        raw_reasons = data.get("partial_reasons", [])
        reasons = ({reason for reason in raw_reasons if isinstance(reason, str) and reason in _PARTIAL_REASONS}
                   if isinstance(raw_reasons, list) else set())
        witnessed = {}
        for location_hit in locations:
            if not isinstance(location_hit, dict) or not isinstance(location_hit.get("path"), str):
                continue
            path = location_hit["path"]
            revision = location_hit.get("revision")
            line, start, end = (location_hit.get(key) for key in ("line", "start_byte", "end_byte"))
            fingerprint = revision.get("value") if isinstance(revision, dict) and revision.get("algorithm") == "sha256" else None
            if (type(line) is int and 1 <= line <= _FILE_BYTES and type(start) is int and type(end) is int
                    and 0 <= start < end <= _FILE_BYTES and end - start == len(needle)
                    and isinstance(fingerprint, str) and re.fullmatch(r"sha256:[a-f0-9]{64}:[0-9]{1,7}", fingerprint)
                    and end <= int(fingerprint.rsplit(":", 1)[-1]) <= _FILE_BYTES):
                witnessed[path] = (line, fingerprint)

        qualified = []
        for path_text in dict.fromkeys(matches):
            if path_text not in witnessed:
                continue
            path = Path(path_text)
            try:
                label = path.relative_to(root).as_posix()
                relative = path.relative_to(location.canonical_path)
                if (not relative.parts or ".." in relative.parts or path.resolve(strict=True) != path
                        or not 1 <= len(label.encode("utf-8")) <= 512):
                    continue
            except (ValueError, OSError):
                continue
            decision = repository.resolve(subject_id=account, is_admin=False, origin="app",
                location_id=location.id, workspace_id=workspace.id, capability="read",
                relative_resource=relative.as_posix(), observation_only=True)
            if decision.allowed and decision.policy_generation == generation:
                qualified.append((path, label, *witnessed[path_text]))
        if binding() != captured:
            raise _unavailable("resource_changed")
        hits = []
        for path, label, line, fingerprint in qualified:
            ref = issue_resource_ref(owner_subject_id=account, provider="host", origin_id=_origin(str(path)),
                kind="file", capabilities=("open",), policy_generation=generation,
                location_id=location.id, workspace_id=workspace.id)
            hits.append({"label": label, "ref": ref.token, "line": line,
                         "revision": {"kind": "hostFingerprint", "value": fingerprint}})
        if len(hits) != len(matches):
            reasons.add("unqualified_match")
        if not data["complete"] and not reasons:
            reasons.add("incomplete_scan")
        return {"version": 1, "workspace_id": workspace.id, "workspace_revision": workspace.revision,
                "state": "partial" if reasons or not data["complete"] else "complete",
                "reasons": sorted(reasons), "hits": hits}
    except (FilePolicyError, WorkspacePolicyServiceError, OSError):
        raise _unavailable() from None
    except (FilesServiceError, ResourceRefError, TimeoutError):
        raise _unavailable("provider_unavailable") from None
