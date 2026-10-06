"""Host-owned managed MiMo history callbacks.

The engine supplies only its current engine session identifier.  ``binding_for``
is responsible for proving that identifier is the admitted current mapping and
returns the owner/stable chat axes; request fields can never select either.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from src.openclank.conversation_archive import ConversationArchive, SourcePart
from src.openclank.managed_protocol import validate_managed_method_request, validate_managed_method_result

QUERY = "_openclank/history/v1/query"
MUTATE = "_openclank/history/v1/mutate"


class ManagedHistoryCallbacks:
    def __init__(self, archive: ConversationArchive, binding_for: Callable[[str], tuple[str, str]]):
        self.archive = archive
        self.binding_for = binding_for

    async def dispatch(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request = validate_managed_method_request(method, dict(params))
        owner, chat_id = self.binding_for(str(request["sessionID"]))
        if method == QUERY:
            result = self._query(owner, chat_id, request)
            return validate_managed_method_result(method, result)
        if method == MUTATE:
            result = self._mutate(owner, chat_id, request)
            return validate_managed_method_result(method, result)
        raise ValueError("unsupported managed history callback")

    def _search_kind_and_tool(self, owner: str, chat_id: str, hit: Mapping[str, Any]) -> tuple[str, str | None]:
        if "history_kind" in hit:
            return str(hit["history_kind"]), (
                str(hit["history_tool_name"]) if hit.get("history_tool_name") else None
            )
        part_type = str(hit.get("part_type") or "")
        if part_type == "text":
            return ("user_text" if hit.get("role") == "user" else "assistant_text"), None
        if part_type == "reasoning":
            return "reasoning", None
        if part_type != "tool":
            return part_type or "assistant_text", None
        full = self.archive.get_part(
            owner=owner,
            chat_id=chat_id,
            actor_id=str(hit.get("actor_id") or "main"),
            message_id=str(hit["message_id"]),
            part_id=str(hit["part_id"]),
        )
        raw = full.get("part", {}).get("text") if full.get("ok") else ""
        return self._part_kind_and_tool(part_type, hit.get("role"), self._mimo_part_payload(raw))

    @staticmethod
    def _mimo_part_payload(raw: Any) -> Mapping[str, Any]:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, Mapping):
            return {}
        migrated = payload.get("mimo")
        if isinstance(migrated, Mapping) and isinstance(migrated.get("part"), Mapping):
            return migrated["part"]
        return payload

    @staticmethod
    def _part_kind_and_tool(part_type: str, role: Any, payload: Mapping[str, Any]) -> tuple[str, str | None]:
        if part_type == "text":
            return ("user_text" if role == "user" else "assistant_text"), None
        if part_type == "reasoning":
            return "reasoning", None
        if part_type != "tool":
            return part_type or "assistant_text", None
        tool_name = payload.get("tool")
        state = payload.get("state")
        status = state.get("status") if isinstance(state, Mapping) else ""
        if status == "error":
            return "tool_error", str(tool_name) if tool_name else None
        if status == "completed":
            return "tool_output", str(tool_name) if tool_name else None
        return "tool_input", str(tool_name) if tool_name else None

    @staticmethod
    def _tool_text(tool_name: str | None, state: Mapping[str, Any] | None) -> str:
        value = state or {}
        if value.get("error"):
            tail = f"error: {value['error']}"
        else:
            tail = f"output: {json.dumps(value.get('output', ''), separators=(',', ':'))}"
        return f"tool: {tool_name or ''}\ninput: {json.dumps(value.get('input', {}), separators=(',', ':'))}\n{tail}"

    def _part_view(self, part: Mapping[str, Any]) -> tuple[str, str | None, str]:
        raw = part.get("text") or ""
        payload = self._mimo_part_payload(raw)
        part_type = str(payload.get("type") or part.get("part_type") or "")
        if part_type == "tool":
            name = str(payload["tool"]) if payload.get("tool") else None
            state = payload.get("state")
            return part_type, name, self._tool_text(name, state if isinstance(state, Mapping) else None)
        if part_type in {"text", "reasoning"} and isinstance(payload.get("text"), str):
            return part_type, None, payload["text"]
        return part_type, None, str(raw)

    def _managed_assets(self, part_id: str, part_type: str, content: Any) -> tuple[Mapping[str, Any], ...]:
        payload = self._mimo_part_payload(content)
        if part_type != "file" or not isinstance(payload.get("url"), str):
            return ()
        url = payload["url"]
        mime = payload.get("mime")
        return ({
            "asset_id": f"mimo-file:{part_id}",
            "mime_type": mime[:255] if isinstance(mime, str) else None,
            "content_hash": "sha256:" + hashlib.sha256(url.encode("utf-8")).hexdigest(),
            "provenance": {"source": "managed-mimo", "field": "url", "inline": url.startswith("data:")},
        },)

    def _query(self, owner: str, chat_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        op = str(request["operation"])
        # A returned legacy hit carries its old archive chat ID. It may be
        # followed only inside the already-derived owner scope; the caller
        # still cannot name an owner or read another account's archive.
        explicit_chat = str(request.get("chatID") or "")
        requested_chat = explicit_chat or chat_id
        if requested_chat != chat_id:
            chat_id = requested_chat
        if op == "search":
            value = self.archive.search(
                owner=owner,
                chat_id=chat_id if str(request.get("scope") or "chat") == "chat" or explicit_chat else None,
                scope=str(request.get("scope") or "chat"),
                query=str(request["query"]),
                limit=int(request.get("limit") or 10),
                kinds=request.get("kind"),
                tool_name=request.get("toolName"),
                time_after=request.get("timeAfter"),
                time_before=request.get("timeBefore"),
            )
            hits = []
            for hit in value.get("hits", []):
                kind, tool_name = self._search_kind_and_tool(owner, str(hit["chat_id"]), hit)
                hits.append({
                    "part_id": str(hit["part_id"]),
                    "session_id": str(hit["chat_id"]),
                    "message_id": str(hit["message_id"]),
                    "project_id": str(hit.get("event_project") or ""),
                    "kind": kind,
                    "tool_name": tool_name,
                    "snippet": str(hit.get("snippet") or ""),
                    "score": 1.0,
                    "time_created": int(hit["time_created"]),
                })
            result = {
                "ok": bool(value.get("ok")),
                "hits": hits,
                "limit": int(value.get("limit") or request.get("limit") or 10),
                "more": bool(value.get("more")),
            }
            if value.get("error"):
                result["error"] = str(value["error"])
        elif op == "around":
            value = self.archive.around(
                owner=owner,
                chat_id=chat_id,
                anchor_message_id=str(request["messageID"]),
                before=int(request.get("before") or 5),
                after=int(request.get("after") or 5),
            )
            result = {
                "ok": bool(value.get("ok")),
                "session_id": chat_id,
                "messages": [
                    {
                        "message_id": str(message["message_id"]),
                        "matched": bool(message["matched"]),
                        "time_created": int(message["time_created"]),
                        "parts": [
                            {
                                "part_id": str(part["part_id"]),
                                "type": view[0],
                                "role": "user" if part.get("role") == "user" else "assistant",
                                "tool_name": view[1],
                                "text": view[2],
                            }
                            for part in message["parts"]
                            for view in [self._part_view(part)]
                        ],
                    }
                    for message in value.get("messages", [])
                ],
            }
            if value.get("error"):
                result["error"] = str(value["error"])
        elif op == "get":
            value = self.archive.get_part(
                owner=owner,
                chat_id=chat_id,
                message_id=str(request["messageID"]),
                part_id=str(request["partID"]),
                offset=int(request.get("offset") or 0),
                length=request.get("length"),
            )
            result = {"ok": bool(value.get("ok"))}
            if value.get("ok"):
                part = value["part"]
                part_type, tool_name, text = self._part_view(part)
                result["part"] = {
                    "part_id": str(part["part_id"]),
                    "message_id": str(part["message_id"]),
                    "session_id": str(part["chat_id"]),
                    "type": part_type,
                    "role": "user" if part.get("role") == "user" else "assistant",
                    "tool_name": tool_name,
                    "text": text,
                    "has_more": bool(value.get("has_more")),
                    "next_offset": value.get("next_offset"),
                    "attachments": [
                        {
                            "asset_id": str(asset["asset_id"]),
                            "mime_type": asset.get("mime_type"),
                            "filename": None,
                            "byte_size": asset.get("byte_size"),
                        }
                        for asset in part.get("assets", [])
                    ],
                    "time_created": int(part["time_created"]),
                }
            elif value.get("error"):
                result["error"] = str(value["error"])
        else:
            value = self.archive.get_media(
                owner=owner,
                chat_id=chat_id,
                asset_id=str(request["assetID"]),
                message_id=request.get("messageID"),
                part_id=request.get("partID"),
            )
            result = {"ok": bool(value.get("ok")), "attachments": []}
            if value.get("ok"):
                asset = value["asset"]
                result["attachments"] = [{
                    "asset_id": str(asset["asset_id"]),
                    "mime_type": asset.get("mime_type"),
                    "filename": None,
                    "byte_size": asset.get("byte_size"),
                }]
            elif value.get("error"):
                result["error"] = str(value["error"])
        return {"ok": bool(result["ok"]), "operation": op, "result": result}

    def _mutate(self, owner: str, chat_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        op = str(request["operation"])
        accepted = duplicate = enqueued = 0
        for event in request["events"]:
            item = dict(event)
            if op == "tombstone":
                revision = item.get("revision")
                if revision is None:
                    previous = self.archive.get_part(
                        owner=owner,
                        chat_id=chat_id,
                        actor_id=str(item.get("actorID") or "main"),
                        message_id=str(item["messageID"]),
                        part_id=str(item["partID"]),
                    )
                    if not previous.get("ok"):
                        duplicate += 1
                        continue
                    revision = previous["part"]["revision"]
                result = self.archive.tombstone_part(owner=owner, chat_id=chat_id, actor_id=str(item.get("actorID") or "main"), message_id=str(item["messageID"]), part_id=str(item["partID"]), revision=int(revision), reason=item.get("tombstoneReason"))
                accepted += int(bool(result.get("tombstoned")))
                duplicate += int(bool(result.get("duplicate")))
                continue
            part_type = str(item.get("partType") or "text")
            part = SourcePart(owner=owner, chat_id=chat_id, actor_id=str(item.get("actorID") or "main"), message_id=str(item["messageID"]), part_id=str(item["partID"]), runtime_generation="managed-mimo-v1", event_sequence=int(item.get("eventSequence") or 0), role=str(item.get("role") or "assistant"), part_type=part_type, content=item.get("content"), time_created=int(item.get("timeCreated") or 0), time_updated=int(item.get("timeUpdated") or 0), assets=self._managed_assets(str(item["partID"]), part_type, item.get("content")))
            result = self.archive.append_managed_parts([part], consumer_hint="managed-mimo-history")
            accepted += int(result["accepted"]); duplicate += int(result["duplicate"]); enqueued += int(result["enqueued"])
        return {"ok": True, "operation": op, "accepted": accepted, "duplicate": duplicate, "enqueued": enqueued}
