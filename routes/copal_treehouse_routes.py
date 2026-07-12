"""Authenticated TreeHouse API mounted inside Copal's existing route adapter."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.openclank.copal_treehouse import (
    TreeHouseError,
    apply_legacy_migration,
    apply_treehouse_command,
    compute_treehouse_projections,
    new_treehouse_state,
    plan_legacy_migration,
    public_treehouse_snapshot,
    state_fingerprint,
    validate_treehouse_state,
)


Call = Callable[[Request, str, dict[str, Any]], Awaitable[Any]]
Scope = Callable[[Request, str | None], dict[str, str]]
Publish = Callable[[dict[str, str], str, dict[str, Any]], None]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TreeHouseCommand(_Strict):
    type: str = Field(min_length=1, max_length=80)
    command_id: str = Field(alias="commandId", min_length=1, max_length=128)
    actor_id: str = Field(alias="actorId", default="owner", min_length=1, max_length=128)
    expected_revision: int | None = Field(alias="expectedRevision", default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class TreeHouseMigration(_Strict):
    command_id: str = Field(alias="commandId", min_length=1, max_length=128)
    actor_id: str = Field(alias="actorId", default="owner", min_length=1, max_length=128)
    expected_revision: int | None = Field(alias="expectedRevision", default=None, ge=0)


def setup_treehouse_routes(*, call: Call, scope_for: Scope, publish: Publish) -> APIRouter:
    router = APIRouter(prefix="/treehouse", tags=["copal-treehouse"])
    state_name = ".copal/treehouse-state.json"

    def fail(exc: TreeHouseError) -> HTTPException:
        return HTTPException(exc.status, detail=exc.payload())

    async def load_state(request: Request, scope: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
        indexed = await call(request, "index", {**scope, "kind": "treehouse-state"})
        docs = [doc for doc in indexed.get("docs", []) if doc.get("name") == state_name]
        if len(docs) > 1:
            raise HTTPException(409, "Multiple TreeHouse state documents exist; repair the workspace before writing")
        if not docs:
            initial = new_treehouse_state(scope["owner"])
            try:
                created = await call(request, "create", {**scope, "name": state_name, "kind": "treehouse-state", "content": json.dumps(initial, separators=(",", ":"))})
                document_id = created.get("doc", {}).get("id")
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                document_id = None
            if not document_id:
                indexed = await call(request, "index", {**scope, "kind": "treehouse-state"})
                match = next((doc for doc in indexed.get("docs", []) if doc.get("name") == state_name), None)
                if not match:
                    raise HTTPException(503, "TreeHouse state could not be initialized")
                document_id = match["id"]
            doc = await call(request, "get", {**scope, "id": document_id})
        else:
            doc = docs[0]
        try:
            state = json.loads(doc.get("text") or "{}")
            validate_treehouse_state(state)
        except (json.JSONDecodeError, TreeHouseError) as exc:
            detail = exc.payload() if isinstance(exc, TreeHouseError) else {"code": "corrupt_state", "message": "TreeHouse state is not valid JSON"}
            raise HTTPException(409, detail=detail) from exc
        return doc, state

    async def write_state(request: Request, scope: dict[str, str], doc: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        content = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
        if len(content.encode()) > 8_388_608:
            raise HTTPException(409, detail={"code": "state_too_large", "message": "TreeHouse state exceeds the Redb document safety limit"})
        result = await call(request, "write", {**scope, "id": doc["id"], "content": content, "base": doc.get("head")})
        if result.get("outcome") == "stale":
            raise HTTPException(409, detail={"code": "stale", "message": "TreeHouse changed in another tab", "revision": state.get("revision")})
        publish(scope, "document", {"treehouse": True, "revision": state["revision"]})
        return result

    @router.get("")
    async def get_treehouse(
        request: Request,
        workspace: str | None = None,
        actor: str = Query("owner", max_length=128),
    ):
        scope = scope_for(request, workspace)
        doc, state = await load_state(request, scope)
        try:
            snapshot = public_treehouse_snapshot(state, actor)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        return {**snapshot, "document": {"id": doc["id"], "head": doc.get("head")}, "workspace": scope["workspace_id"], "fingerprint": state_fingerprint(state)}

    @router.post("/commands")
    async def command_treehouse(
        command: TreeHouseCommand,
        request: Request,
        workspace: str | None = None,
    ):
        scope = scope_for(request, workspace)
        doc, state = await load_state(request, scope)
        try:
            next_state, result, changed = apply_treehouse_command(
                state,
                {"type": command.type, "payload": command.payload},
                actor_id=command.actor_id,
                command_id=command.command_id,
                expected_revision=command.expected_revision,
            )
            if changed:
                await write_state(request, scope, doc, next_state)
            snapshot = public_treehouse_snapshot(next_state, command.actor_id)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        return {"ok": True, "changed": changed, "result": result, **snapshot}

    @router.post("/migrate")
    async def migrate_treehouse(
        migration: TreeHouseMigration,
        request: Request,
        workspace: str | None = None,
        dry_run: bool = Query(True),
    ):
        scope = scope_for(request, workspace)
        doc, state = await load_state(request, scope)
        indexed = await call(request, "index", scope)
        plan = plan_legacy_migration(indexed.get("docs", []), state)
        if dry_run:
            return {"ok": True, "dryRun": True, "plan": plan, "revision": state["revision"]}
        try:
            next_state, result, changed = apply_legacy_migration(
                state,
                plan,
                actor_id=migration.actor_id,
                command_id=migration.command_id,
                expected_revision=migration.expected_revision,
            )
            if changed:
                await write_state(request, scope, doc, next_state)
            snapshot = public_treehouse_snapshot(next_state, migration.actor_id)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        return {"ok": True, "dryRun": False, "changed": changed, "result": result, "plan": plan, **snapshot}

    @router.get("/integrity")
    async def treehouse_integrity(request: Request, workspace: str | None = None):
        scope = scope_for(request, workspace)
        _, state = await load_state(request, scope)
        try:
            projection = compute_treehouse_projections(state)
        except TreeHouseError as exc:
            raise fail(exc) from exc
        return {"ok": True, "schemaVersion": state["schemaVersion"], "revision": state["revision"], "eventCount": len(state["events"]), "projectionLearners": len(projection["learners"]), "fingerprint": state_fingerprint(state)}

    return router
