"""Typed Copal-to-Lore capture metadata shared by every mutation adapter.

Copal owns native head versions; this translates their identity into Lore wire
metadata without making Lore the native version authority.
"""
from __future__ import annotations
from typing import Any, Mapping, TypedDict

class OpaqueRevision(TypedDict):
    kind: str
    value: str

class Revision(TypedDict):
    Opaque: OpaqueRevision

class Locator(TypedDict):
    display_name: str
    location_label: str
    opaque_ref: str | None

def revision(head: Any) -> Revision | None:
    return {"Opaque": {"kind": "head", "value": str(head)}} if head is not None and str(head) else None

def locator(name: Any, workspace: str, resource_id: str) -> Locator:
    return {"display_name": str(name or resource_id), "location_label": workspace, "opaque_ref": resource_id}

def capture_metadata(before: Mapping[str, Any] | None, *, workspace: str,
                     resource_id: str, destination_name: Any = None) -> dict[str, Any]:
    head = revision(before.get("head")) if before is not None else None
    return {
        "before_revision": head,
        "expected_revision": head,
        "original_locator": locator(before.get("name"), workspace, resource_id) if before is not None else None,
        "destination_locator": locator(destination_name or (before or {}).get("name"), workspace, resource_id),
    }

def mutation_failure_status(error: BaseException) -> str:
    # Only an acknowledged provider rejection proves no commit. Lost response,
    # cancellation or untyped exceptions require reconciliation of this action.
    return "NotCommitted" if getattr(error, "mutation_outcome", None) == "NotCommitted" else "Unknown"

def settle_terminal_recovery(client: Any, action_id: str, response: Any) -> bool:
    action = response.get("Action") if isinstance(response, Mapping) else None
    if not isinstance(action, Mapping) or action.get("recovery_only") is not True:
        return False
    if action.get("state") not in {"Complete", "Failed", "Conflict", "Aborted", "AfterCaptureFailed"}:
        return False
    client.settle_recovery(action_id)
    return True


def finish_capture(client: Any, action_id: str, response: Any) -> dict[str, Any]:
    """Release temporary recovery bytes after the service verifies a terminal mutation."""
    if settle_terminal_recovery(client, action_id, response):
        return {"status": "skipped", "durable": False, "phase": "recovery_settled"}
    return {"status": "complete", "durable": True, "phase": "complete"}
