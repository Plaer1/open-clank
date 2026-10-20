"""Workspace API - browse server directories to pick a tool workspace folder."""
import os
from fastapi import APIRouter, Request, HTTPException, Query

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user
from src.openclank.filesystem_registry import FilesystemRegistryError, FilesystemRootRegistry

# Cap entries returned per directory (mirrors filesystem_tools._CODENAV_MAX_HITS).
# A huge directory shouldn't dump thousands of rows into the picker; the user can
# type/paste a path to jump straight in instead.
_MAX_BROWSE_DIRS = 500
_SELECTION_KINDS = {"agent_workspace", "app_folder"}


def setup_workspace_routes():
    router = APIRouter(prefix="/api/workspace", tags=["workspace"])
    registry = FilesystemRootRegistry()

    def visible_recursive_roots(owner: str) -> list[dict]:
        try:
            return [
                item
                for item in registry.visibility_for_subject(owner)
                if item.get("root", {}).get("kind") == "recursive_directory"
                and item.get("root", {}).get("enabled")
                and item.get("root", {}).get("availability") == "available"
                # Directory enumeration and workspace selection are read
                # operations. A write-only/execute-only assignment must not
                # leak child names through this compatibility picker.
                and "read" in set(item.get("capabilities") or [])
                and "read" in set(item.get("root", {}).get("capabilities") or [])
            ]
        except FilesystemRegistryError as error:
            raise HTTPException(status_code=503, detail={"code": error.code, "message": str(error)}) from error

    def _contains(root: str, target: str) -> bool:
        try:
            return os.path.commonpath([root, target]) == root
        except ValueError:
            return False

    def visible_parent(target: str, *, is_admin: bool, visible_roots: list[dict]):
        """Return only a parent the caller is authorized to enumerate."""
        parent = os.path.dirname(target)
        if not parent or parent == target:
            return None
        if is_admin:
            return parent
        # The virtual assigned-roots screen is the navigation boundary for a
        # standard user. At an assigned root its host parent must stay hidden;
        # below that root, ordinary upward navigation remains available.
        return parent if any(
            _contains(str((item.get("root") or {}).get("canonical_path") or ""), parent)
            for item in visible_roots
        ) else None

    @router.get("/browse")
    def browse(
        request: Request,
        path: str = Query(default=""),
        selection_kind: str = Query(default="agent_workspace"),
    ):
        """List subdirectories of `path` (default: home) so the UI can navigate
        the server filesystem and pick a workspace folder. Directories only.

        Administrators enumerate their OS-visible namespace. Non-admins start
        from a virtual list of administrator-issued recursive roots and can
        enumerate only descendants of those roots; no assignment is an empty
        result/deny state.
        """
        selection = selection_kind if isinstance(selection_kind, str) else "agent_workspace"
        if selection not in _SELECTION_KINDS:
            raise HTTPException(status_code=400, detail="selection_kind must be agent_workspace or app_folder")
        owner = get_current_user(request)
        is_admin = owner_is_admin_or_single_user(owner)
        visible_roots = [] if is_admin else visible_recursive_roots(str(owner))
        if not is_admin and not path.strip():
            # A non-admin's picker starts at a virtual list of assigned roots;
            # it never probes home or a filesystem parent to discover them.
            dirs = []
            seen = set()
            for item in visible_roots:
                root = item.get("root") or {}
                canonical = str(root.get("canonical_path") or "")
                if not canonical or canonical in seen:
                    continue
                seen.add(canonical)
                dirs.append({"name": os.path.basename(canonical) or canonical, "path": canonical})
            dirs.sort(key=lambda item: item["name"].casefold())
            return {"path": "", "parent": None, "dirs": dirs[:_MAX_BROWSE_DIRS], "truncated": len(dirs) > _MAX_BROWSE_DIRS, "selectable": False}
        from src.tool_execution import vet_workspace

        # Resolve and authorize the requested location before invoking either
        # filesystem implementation. In particular, a standard user's path
        # must never reach the optional service adapter unless it is already
        # inside one of their assigned roots; otherwise response differences
        # could turn this browser into a host-path existence oracle.
        target = os.path.realpath(os.path.expanduser(path.strip() or "~"))
        if not is_admin and not any(
            _contains(str((item.get("root") or {}).get("canonical_path") or ""), target)
            for item in visible_roots
        ):
            raise HTTPException(status_code=403, detail="Workspace is outside assigned user-visible roots")

        # Opt-in compatibility adapter: when the Rust service is enabled, the
        # existing picker keeps its response shape while directory metadata is
        # supplied by the long-lived app-principal service. The legacy path
        # remains the safe fallback until the service is packaged by default.
        if os.environ.get("ODYSSEUS_FILES_WORKSPACE_ADAPTER", "0") == "1":
            from src.openclank.files_service_client import FilesServiceError, client_for_owner
            scope = registry.app_scope(str(owner), is_admin=is_admin)
            try:
                service_client = client_for_owner(str(owner)) if scope.get("host") else client_for_owner(str(owner), app_scope=scope)
                response = _run_service_browse(service_client, target)
                entries = ((response.get("data") or {}).get("entries") or [])
                dirs = [
                    {"name": entry.get("name", ""), "path": os.path.join(target, entry.get("name", ""))}
                    for entry in entries
                    if str(entry.get("kind", "")).lower().endswith("directory")
                    and (
                        selection == "app_folder"
                        or not str(entry.get("name", "")).startswith(".")
                    )
                ]
                return {
                    "path": target,
                    "parent": visible_parent(target, is_admin=is_admin, visible_roots=visible_roots),
                    "dirs": dirs,
                    "truncated": bool((response.get("data") or {}).get("next_cursor")),
                    # The Code Editor's app folder is user-directed UI state,
                    # not an agent confinement boundary. It may therefore use
                    # any directory inside the authenticated app scope,
                    # including a filesystem root for an administrator.
                    "selectable": (
                        True
                        if selection == "app_folder"
                        else vet_workspace(target) is not None
                    ),
                }
            except FilesServiceError as error:
                if error.code != "root_unavailable":
                    raise HTTPException(status_code=503, detail=str(error)) from error

        # Resolve symlinks so the reported path is canonical and the UI navigates
        # real directories (defends against symlink games in displayed paths).
        if not os.path.isdir(target):
            if is_admin:
                target = os.path.realpath(os.path.expanduser("~"))
            else:
                raise HTTPException(status_code=403, detail="Workspace is outside assigned user-visible roots")

        dirs = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        # Don't follow symlinks when classifying - a symlinked
                        # dir is skipped rather than letting the browser wander
                        # off via a link. Agent workspace browsing keeps the
                        # legacy hidden-folder omission; the user-directed Code
                        # picker shows every directory in the app-visible scope.
                        if entry.is_dir(follow_symlinks=False) and (
                            selection == "app_folder" or not entry.name.startswith(".")
                        ):
                            # Build the child path server-side with os.path.join
                            # so it's correct on Windows (backslashes) and Linux.
                            dirs.append({"name": entry.name, "path": os.path.join(target, entry.name)})
                    except OSError:
                        continue
        except (PermissionError, OSError):
            dirs = []

        dirs_sorted = sorted(dirs, key=lambda d: d["name"].lower())
        truncated = len(dirs_sorted) > _MAX_BROWSE_DIRS
        return {
            "path": target,
            "parent": visible_parent(target, is_admin=is_admin, visible_roots=visible_roots),
            "dirs": dirs_sorted[:_MAX_BROWSE_DIRS],
            "truncated": truncated,
            # Whether this directory may be bound as a workspace (filesystem
            # roots and sensitive dirs may be browsed through but not chosen).
            "selectable": (
                is_admin
                or any(
                    _contains(str((item.get("root") or {}).get("canonical_path") or ""), target)
                    for item in visible_roots
                )
            ) and (
                selection == "app_folder"
                or vet_workspace(target) is not None
            ),
        }

    @router.get("/vet")
    def vet(request: Request, path: str = Query(default="")):
        """Validate a workspace path without binding it.

        The UI calls this before persisting a manually typed path (/workspace
        set) so a typo, file path, deleted folder, sensitive dir, or filesystem
        root is rejected up front with the canonical path returned on success,
        instead of being stored client-side and silently dropped at chat time.
        The server confirms path existence only inside the caller's effective
        app scope; paths outside a non-admin assignment are rejected uniformly.
        """
        owner = get_current_user(request)
        is_admin = owner_is_admin_or_single_user(owner)
        from src.tool_execution import vet_workspace
        if not is_admin:
            roots = visible_recursive_roots(str(owner))
            # Do the assignment containment check before vetting existence,
            # sensitivity, or directory shape. A standard user must not be
            # able to turn this compatibility endpoint into an existence or
            # metadata oracle for an unassigned absolute path.
            candidate = os.path.realpath(os.path.expanduser(str(path or "").strip())) if str(path or "").strip() else ""
            if not candidate or not any(
                _contains(str((item.get("root") or {}).get("canonical_path") or ""), candidate)
                for item in roots
            ):
                raise HTTPException(status_code=403, detail="Workspace is outside assigned user-visible roots")
            resolved = vet_workspace(candidate)
            if resolved is None or not any(
                _contains(str((item.get("root") or {}).get("canonical_path") or ""), resolved)
                for item in roots
            ):
                raise HTTPException(status_code=403, detail="Workspace is outside assigned user-visible roots")
        else:
            resolved = vet_workspace(path)
        return {"ok": resolved is not None, "path": resolved}

    return router


def _run_service_browse(client, target):
    """Bridge async Rust client to this legacy sync route without changing its API."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # FastAPI executes this sync endpoint in a worker thread, so this branch
        # is defensive only; a running loop would indicate an unusual caller.
        raise RuntimeError("workspace adapter cannot run inside an active event loop")
    return asyncio.run(client.request("list_directory", target, {}))
