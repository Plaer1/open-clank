"""Editor identity for Copal records, shared with the authorized Files view.

Descriptors describe an already authorized record. They are never a substitute
for the provider's read/write checks and contain no provider origin identifier.
"""

from __future__ import annotations

from typing import Any, Mapping

from src.openclank.resource_refs import stable_resource_id


def copal_resource_descriptor(
    document: Mapping[str, Any],
    *,
    owner_account_id: str,
    workspace_id: str,
    writable_adapter: bool = True,
) -> dict[str, Any]:
    """Use exactly the same origin identity as ``CopalFilesProvider``.

    Names, mutable usernames, storage heads and rotating access refs do not
    participate in equality. The account and workspace remain explicit so a
    shared resource owner cannot be confused with its authenticated editor.
    """
    document_id = str(document.get("id") or "").strip()
    owner = str(owner_account_id or "").strip()
    workspace = str(workspace_id or "").strip()
    if not document_id or not owner or not workspace:
        raise ValueError("Copal resource identity requires owner, workspace and document")
    kind = str(document.get("kind") or "note").lower()
    head = str(document.get("head") or "")
    preserved = bool(document.get("rawPreserved") or document.get("note_error"))
    read_only = bool(document.get("readOnly") or document.get("builtin") or preserved)
    editable = kind in {"note", "wiki", "markdown", "base"} and bool(head) and not read_only
    representation = {
        "note": "nativeNote", "wiki": "meme", "markdown": "markdown",
        "base": "base", "asset": "asset",
    }.get(kind, "text")
    return {
        "key": {
            "accountId": owner,
            "workspaceId": workspace,
            "provider": "copal",
            "resourceId": stable_resource_id(
                owner_subject_id=owner,
                provider="copal",
                origin_id=f"document:{workspace}:{document_id}",
            ),
        },
        "revision": {"kind": "copalHead", "value": head},
        "representation": representation,
        "locator": {
            "displayName": str(document.get("name") or "Untitled"),
            "locationLabel": workspace,
        },
        "capabilities": {
            "read": True,
            "edit": editable and writable_adapter,
            "rename": editable and writable_adapter,
            "move": editable and writable_adapter,
            "trash": editable and writable_adapter,
            "attach": kind == "asset" and not read_only,
            "reveal": True,
        },
    }
