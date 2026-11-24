"""Authenticated workspace events for the existing Memery index owner.

This adapter owns no jobs or persistence. Roots and authority revisions are
resolved from canonical FilePolicy state, never accepted from an index client.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from src.memory_provider import MemoryRequestRejectedError, MemoryTransportError
from src.openclank.file_policy import FilePolicyError, FilePolicyRepository
from src.openclank.workspace_policy_service import (
    WorkspacePolicyServiceError,
    resolve_owned_workspace,
)


class CodeIndexLifecycleError(ValueError):
    def __init__(self, message: str, *, code: str = "index_unavailable"):
        super().__init__(message)
        self.code = code


def _public_status(result, private_root):
    """Redact diagnostic prose, retaining canonical identities verbatim."""
    def diagnostic(value):
        return value.replace(private_root, "[workspace]") if private_root and isinstance(value, str) else value

    fields = {"registration_id", "checkout_id", "admission_epoch", "run_id",
        "head_generation", "lifecycle", "freshness", "config_fingerprint", "coverage_complete", "budget_omitted_files"}
    public = {key: result[key] for key in fields if key in result}
    if "coverage" in result:
        coverage_fields = {"rel_path", "status", "detail", "hash", "generation", "source_hash", "source_generation"}
        public["coverage"] = [{key: diagnostic(value) if key == "detail" else value
            for key, value in row.items() if key in coverage_fields} for row in result["coverage"]]
    if "progress" in result:
        progress = result["progress"] or {}
        public["progress"] = {key: progress[key] for key in ("completed_files", "total_files", "heartbeat_at") if key in progress}
        phases = {"inventory", "verification", "extraction", "dependency_resolution", "visual_summary", "sealing", "publication", "ready"}
        if progress.get("phase") in phases:
            public["progress"]["phase"] = progress["phase"]
            for key in ("phase_completed", "phase_total"):
                value = progress.get(key)
                if type(value) is int and 0 <= value <= 4_000_000:
                    public["progress"][key] = value
            if progress.get("phase_unit") in {"files", "entries", "references", "links"}:
                public["progress"]["phase_unit"] = progress["phase_unit"]
            observed = progress.get("phase_observed_at")
            if isinstance(observed, str) and len(observed) <= 64:
                public["progress"]["phase_observed_at"] = observed
    if "error" in result:
        error = result["error"]
        public["error"] = {key: diagnostic(value) if key == "message" else value
            for key, value in error.items() if key in {"code", "message"}} if error is not None else None
    if "removal_review" in result:
        review = result["removal_review"]
        keys = {"required", "prior_generation", "prior_files", "removed_files", "minimum_files",
                "minimum_percent", "sample_paths", "confirmation"}
        if not isinstance(review, dict) or set(review) != keys or type(review["required"]) is not bool:
            raise CodeIndexLifecycleError("Invalid index removal preview", code="invalid_response")
        if any(type(review[key]) is not int or not 0 <= review[key] <= 10_000
               for key in ("prior_files", "removed_files", "minimum_files", "minimum_percent")):
            raise CodeIndexLifecycleError("Invalid index removal counts", code="invalid_response")
        samples = review["sample_paths"]
        if not isinstance(samples, list) or len(samples) > 16 or any(
                not isinstance(path, str) or not path or len(path) > 4096 or path.startswith(("/", "\\"))
                for path in samples):
            raise CodeIndexLifecycleError("Invalid index removal paths", code="invalid_response")
        confirmation = review["confirmation"]
        if review["required"]:
            if not isinstance(confirmation, str) or len(confirmation) != 64 or any(c not in "0123456789abcdef" for c in confirmation):
                raise CodeIndexLifecycleError("Invalid index removal confirmation", code="invalid_response")
        elif confirmation is not None:
            raise CodeIndexLifecycleError("Invalid index removal confirmation", code="invalid_response")
        public["removal_review"] = dict(review)
    return public


async def workspace_index_event(
    *, repository: FilePolicyRepository, provider: object, auth_manager: object,
    owner: str, workspace_id: str, subscriber_id: str, action: str = "ensure",
    purpose: str = "agent_workspace", session: object | None = None, removal_confirmation: str | None = None,
    replace_stale_root: bool = False,
) -> dict:
    """Resolve current authority before calling and returning native status."""
    if action not in {"ensure", "status", "release", "cancel", "remove", "admit", "preview_removals", "confirm_removals"}:
        raise CodeIndexLifecycleError("Unsupported index action", code="invalid_action")
    if purpose not in {"agent_workspace", "app_folder"}:
        raise CodeIndexLifecycleError("Unsupported workspace purpose", code="invalid_purpose")
    if action in {"preview_removals", "confirm_removals"} and (purpose != "app_folder" or session is not None):
        raise CodeIndexLifecycleError("Index removal review requires the app workspace", code="scope_denied")
    if (action == "confirm_removals") != (removal_confirmation is not None):
        raise CodeIndexLifecycleError("Index removal confirmation is required", code="invalid_action")
    if replace_stale_root and (action != 'ensure' or purpose != 'app_folder' or session is not None):
        raise CodeIndexLifecycleError('Root replacement requires an explicit app folder open', code='scope_denied')
    if session is not None:
        if getattr(session, "owner", None) != owner:
            raise CodeIndexLifecycleError("Chat is unavailable", code="scope_denied")
        if getattr(session, "incognito", False):
            raise CodeIndexLifecycleError("Code indexing is disabled for incognito chats", code="incognito")
        if action != "release" and getattr(session, "workspace_id", None) != workspace_id:
            raise CodeIndexLifecycleError("Chat Workspace has changed", code="stale_binding")
        if action in {"ensure", "status", "admit"} and getattr(session, "archived", False):
            raise CodeIndexLifecycleError("Chat is closed", code="stale_binding")
    invoke = getattr(provider, "code_index_lifecycle", None)
    if not callable(invoke):
        raise CodeIndexLifecycleError("Code indexing is unavailable")

    def resolve():
        if action in {"release", "cancel", "remove"}:
            account_id = getattr(auth_manager, "account_id", lambda _: None)(owner)
            if not account_id:
                raise WorkspacePolicyServiceError("Workspace is unavailable", code="workspace_unavailable")
            identifier = repository.legacy_target("workspace", str(account_id) + ":" + workspace_id, fallback=workspace_id)
            try:
                workspace = repository.get_workspace(identifier)
            except FilePolicyError as error:
                raise WorkspacePolicyServiceError("Workspace is unavailable", code="workspace_unavailable") from error
            if workspace.owner_subject_id != str(account_id):
                raise WorkspacePolicyServiceError("Workspace is unavailable", code="workspace_unavailable")
            return workspace, None
        binding = resolve_owned_workspace(
            repository, workspace_id=workspace_id, owner_username=owner,
            auth_manager=auth_manager, purpose=purpose,
        )
        return binding.workspace, binding.path

    workspace, root = resolve()
    generation = repository.generation()
    scope = dict(owner=owner, owner_subject_id=workspace.owner_subject_id,
        workspace_id=workspace.id, location_id=workspace.location_id,
        workspace_revision=workspace.revision, policy_generation=generation,
        subscriber_id=subscriber_id, origin="agent" if purpose == "agent_workspace" else "app",
    )
    extra = {"removal_confirmation": removal_confirmation} if removal_confirmation is not None else {}
    if replace_stale_root:
        extra['replace_stale_root'] = True
    result = await invoke(action=action, root=root, **scope, **extra)
    # A result that completed after revocation/rebinding cannot re-populate
    # this consumer. Native independently fences admission and publication.
    try:
        current, current_root = resolve()
        if (current.revision != workspace.revision
                or current_root != root or repository.generation() != generation
                or (session is not None and action != "release"
                    and (getattr(session, "workspace_id", None) != workspace_id
                        or (action in {"ensure", "status", "admit"} and getattr(session, "archived", False))))):
            raise CodeIndexLifecycleError("Workspace authority changed", code="stale_binding")
    except (CodeIndexLifecycleError, WorkspacePolicyServiceError, FilePolicyError, sqlite3.Error):
        if action in {"ensure", "confirm_removals"}:
            try:
                # An earlier close/rebind may have released before the native
                # open registered. Detach that exact old scope again on ACK.
                await invoke(action="release", root=None, **scope)
            except Exception:
                logging.getLogger(__name__).warning("Could not release stale code index subscription")
        raise
    if not isinstance(result, dict):
        raise CodeIndexLifecycleError("Invalid index response", code="invalid_response")
    # Native metadata is deliberately projected; private roots, identity
    # receipts and authority database paths never cross the browser boundary.
    # These are the resolved public scope identities revalidated above, not
    # caller/native claims. Consumers need them to fence delayed source hints
    # and retain only same-scope published metadata during verification.
    return {"workspace_id": workspace.id, "workspace_revision": workspace.revision,
            "policy_generation": generation, **_public_status(result, root)}


def workspace_index_changes(*, repository, auth_manager, owner, workspace_id, session_current):
    """Scope Files' existing path-free watch to the exact Graph Workspace."""
    from src.openclank.files_service_client import FilesServiceError, client_for_owner

    def resolve():
        return resolve_owned_workspace(repository, workspace_id=workspace_id,
            owner_username=owner, auth_manager=auth_manager, purpose="app_folder")

    binding = resolve()
    workspace, root = binding.workspace, binding.path
    generation = repository.generation()
    location = workspace.location_id
    scope = dict(host=False, visible_root_ids=[location], capabilities=["read"],
        root_capabilities={location: ["read"]}, generation=generation,
        active_folder=dict(id="code-" + workspace.id, root_id=location, canonical_path=root, capabilities=["read"]))

    def current():
        try:
            if not session_current():
                return False
            latest = resolve()
            return (repository.generation() == generation and latest.path == root
                and latest.workspace.revision == workspace.revision
                and latest.workspace.owner_subject_id == workspace.owner_subject_id
                and latest.workspace.location_id == location)
        except (WorkspacePolicyServiceError, FilePolicyError, sqlite3.Error):
            return False

    async def changes():
        # No second watcher implementation. Native Files owns the handle and
        # drops it with this stream. Hints carry no paths, so authoritative
        # reconciliation uses the normal bounded index owner.
        if not current():
            return
        client = client_for_owner(owner, app_scope=scope, transport="grpc")
        events = client.watch_path(root)
        pending = asyncio.create_task(anext(events))
        try:
            while current():
                done, _ = await asyncio.wait({pending}, timeout=15)
                if not current():
                    return
                if not done:
                    yield b": keepalive\n\n"
                    continue
                try:
                    pending.result()
                except (StopAsyncIteration, FilesServiceError):
                    return
                pending = asyncio.create_task(anext(events))
                deadline = asyncio.get_running_loop().time() + .25
                for _ in range(31):
                    done, _ = await asyncio.wait({pending}, timeout=max(0, deadline-asyncio.get_running_loop().time()))
                    if not done:
                        break
                    try:
                        pending.result()
                    except (StopAsyncIteration, FilesServiceError):
                        if current():
                            yield b"event: index-change\ndata: rescan\n\n"
                        return
                    pending = asyncio.create_task(anext(events))
                if current():
                    yield b"event: index-change\ndata: rescan\n\n"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await events.aclose()

    return changes()


async def session_workspace_index_event(request, session, *, action="ensure", workspace_id=None):
    """Best-effort availability hook after a real chat binding change."""
    if getattr(session, "incognito", False):
        return None
    selected = workspace_id or getattr(session, "workspace_id", None)
    if not selected:
        return None
    state = request.app.state
    try:
        return await workspace_index_event(
            repository=FilePolicyRepository(), provider=getattr(state, "memory_provider", None),
            auth_manager=getattr(state, "auth_manager", None), owner=session.owner,
            workspace_id=selected, subscriber_id="session:" + str(session.id),
            action=action, session=session,
        )
    except (CodeIndexLifecycleError, WorkspacePolicyServiceError, FilePolicyError) as error:
        return {"lifecycle": "unavailable", "error": {"code": error.code, "message": str(error)}}
    except sqlite3.Error:
        return {"lifecycle": "unavailable", "error": {"code": "index_unavailable", "message": "Workspace index authority is unavailable"}}
    except (MemoryRequestRejectedError, MemoryTransportError):
        logging.getLogger(__name__).warning("Code index owner unavailable for session Workspace")
        return {"lifecycle": "unavailable", "error": {"code": "index_unavailable", "message": "Code index owner is unavailable"}}
