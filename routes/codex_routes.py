"""Codex integration routes.

These are small HTTP surfaces intended for the Codex plugin/MCP bridge. They
reuse existing Odysseus helpers and enforce API-token scopes before touching
user data.
"""

import json
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.auth_helpers import require_user
from src.tool_implementations import do_manage_notes


TODO_READ_SCOPES = {"todos:read", "todos:write"}
TODO_WRITE_SCOPES = {"todos:write"}
EMAIL_READ_SCOPES = {"email:read", "email:draft", "email:send"}
WRITE_ACTIONS = {"add", "create", "new", "save", "remind", "update", "delete", "toggle_item", "remove", "remove_item"}


def _scope_owner(request: Request, allowed: set[str]) -> str:
    """Return the data owner if the caller is allowed for this Codex action."""
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if not scopes.intersection(allowed):
            required = " or ".join(sorted(allowed))
            raise HTTPException(403, f"API token missing required scope: {required}")
        owner = getattr(request.state, "api_token_owner", None)
        if not owner:
            raise HTTPException(403, "API token has no owner")
        return owner
    return require_user(request)


def _find_endpoint(router: APIRouter | None, method: str, path: str):
    if router is None:
        return None
    for route in getattr(router, "routes", []):
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    return None


def setup_codex_routes(email_router: APIRouter | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/codex", tags=["codex"])
    email_list_endpoint = _find_endpoint(email_router, "GET", "/api/email/list")
    email_read_endpoint = _find_endpoint(email_router, "GET", "/api/email/read/{uid}")

    @router.get("/capabilities")
    def capabilities(request: Request):
        token_scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        has_token = bool(getattr(request.state, "api_token", False))
        return {
            "integration": "codex",
            "token_scopes": sorted(token_scopes),
            "tools": {
                "todos": {
                    "read": bool(token_scopes.intersection(TODO_READ_SCOPES)) if has_token else True,
                    "write": bool(token_scopes.intersection(TODO_WRITE_SCOPES)) if has_token else True,
                    "actions": ["list", "add", "update", "delete", "toggle_item"],
                },
                "email": {
                    "read": bool(token_scopes.intersection(EMAIL_READ_SCOPES)) if has_token else True,
                    "draft": "email:draft" in token_scopes if has_token else True,
                    "send": "email:send" in token_scopes if has_token else True,
                    "actions": ["list", "read"],
                }
            },
            "safety": {
                "email_send_requires_confirmation": True,
                "destructive_actions_should_confirm": True,
            },
        }

    @router.get("/plugin.zip")
    def plugin_zip(request: Request):
        require_user(request)
        root = Path(__file__).resolve().parent.parent / "integrations" / "codex"
        if not root.exists():
            raise HTTPException(404, "Codex plugin bundle not found")
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                zf.write(path, Path("odysseus") / path.relative_to(root))
        buf.seek(0)
        headers = {"Content-Disposition": 'attachment; filename="odysseus-codex-plugin.zip"'}
        return StreamingResponse(buf, media_type="application/zip", headers=headers)

    @router.get("/todos")
    async def list_todos(request: Request, archived: bool = False, label: str | None = None):
        owner = _scope_owner(request, TODO_READ_SCOPES)
        args: dict[str, Any] = {"action": "list", "archived": archived}
        if label:
            args["label"] = label
        return await do_manage_notes(json.dumps(args), owner=owner)

    @router.post("/todos")
    async def manage_todos(request: Request, body: dict[str, Any] = Body(default_factory=dict)):
        action = str(body.get("action") or "add").replace("-", "_").strip().lower()
        allowed = TODO_WRITE_SCOPES if action in WRITE_ACTIONS else TODO_READ_SCOPES
        owner = _scope_owner(request, allowed)
        args = dict(body)
        args["action"] = action
        return await do_manage_notes(json.dumps(args), owner=owner)

    @router.get("/emails")
    async def list_emails(
        request: Request,
        folder: str = "INBOX",
        limit: int = 10,
        offset: int = 0,
        filter: str = "all",
        from_addr: str | None = None,
        account_id: str | None = None,
        has_attachments: int = 0,
    ):
        owner = _scope_owner(request, EMAIL_READ_SCOPES)
        if email_list_endpoint is None:
            raise HTTPException(503, "Email integration is not available")
        limit = max(1, min(int(limit or 10), 50))
        offset = max(0, int(offset or 0))
        if account_id:
            from routes.email_helpers import _assert_owns_account

            _assert_owns_account(account_id, owner)
        return await email_list_endpoint(
            folder=folder,
            limit=limit,
            offset=offset,
            filter=filter,
            from_addr=from_addr,
            account_id=account_id,
            has_attachments=has_attachments,
            cache_bust=None,
            owner=owner,
        )

    @router.get("/emails/{uid}")
    async def read_email(
        request: Request,
        uid: str,
        folder: str = "INBOX",
        account_id: str | None = None,
        mark_seen: bool = False,
    ):
        owner = _scope_owner(request, EMAIL_READ_SCOPES)
        if email_read_endpoint is None:
            raise HTTPException(503, "Email integration is not available")
        if account_id:
            from routes.email_helpers import _assert_owns_account

            _assert_owns_account(account_id, owner)
        return await email_read_endpoint(
            uid=uid,
            folder=folder,
            account_id=account_id,
            mark_seen=mark_seen,
            owner=owner,
        )

    return router
