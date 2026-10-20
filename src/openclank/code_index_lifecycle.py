"""Authenticated workspace events for the existing Frankenmemory index owner.

This adapter owns no jobs or persistence. Roots and authority revisions are
resolved from canonical FilePolicy state, never accepted from an index client.
"""

from __future__ import annotations

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
        "head_generation", "lifecycle", "freshness", "config_fingerprint", "coverage_complete"}
    public = {key: result[key] for key in fields if key in result}
    if "coverage" in result:
        coverage_fields = {"rel_path", "status", "detail", "hash", "generation", "source_hash", "source_generation"}
        public["coverage"] = [{key: diagnostic(value) if key == "detail" else value
            for key, value in row.items() if key in coverage_fields} for row in result["coverage"]]
    if "progress" in result:
        progress = result["progress"] or {}
        public["progress"] = {key: progress[key] for key in ("completed_files", "total_files", "heartbeat_at") if key in progress}
    if "error" in result:
        error = result["error"]
        public["error"] = {key: diagnostic(value) if key == "message" else value
            for key, value in error.items() if key in {"code", "message"}} if error is not None else None
    return public


async def workspace_index_event(
    *, repository: FilePolicyRepository, provider: object, auth_manager: object,
    owner: str, workspace_id: str, subscriber_id: str, action: str = "ensure",
    purpose: str = "agent_workspace", session: object | None = None,
) -> dict:
    """Resolve current authority before calling and returning native status."""
    if action not in {"ensure", "status", "release", "cancel", "remove", "admit"}:
        raise CodeIndexLifecycleError("Unsupported index action", code="invalid_action")
    if purpose not in {"agent_workspace", "app_folder"}:
        raise CodeIndexLifecycleError("Unsupported workspace purpose", code="invalid_purpose")
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
    result = await invoke(action=action, root=root, **scope)
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
        if action == "ensure":
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
    return {"workspace_id": workspace.id, **_public_status(result, root)}


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
