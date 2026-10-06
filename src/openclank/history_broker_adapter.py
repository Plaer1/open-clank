"""Scoped history broker adapter over the conversation archive.

Exposes the shared typed history surface (``history.search`` / ``history.around``
/ ``history.get`` / ``history.media``) through the existing Frankenmemory
broker pattern. Scope always derives from the authenticated binding; same-owner
``global`` scope is not cross-account scope. Guessed ids fail closed.

No new daemon. Frankenmemory remains the retrieval surface for authored memory;
this adapter is the conversation-history surface only. Lore remains the recovery
authority for versions. On shared-service failure the adapter returns an
explicit recoverable-unavailable result and never falls back to an unscoped
native store.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Optional, Sequence

from src.openclank.conversation_archive import (
    AROUND_AFTER_MAX,
    AROUND_BEFORE_MAX,
    GET_LENGTH_MAX_UTF16,
    SEARCH_LIMIT_MAX,
    ArchiveUnavailableError,
    ConversationArchive,
    SourcePart,
    get_conversation_archive,
)


HISTORY_SCHEMA_VERSION = 1

BOUNDS = {
    "search_limit_max": SEARCH_LIMIT_MAX,
    "around_before_max": AROUND_BEFORE_MAX,
    "around_after_max": AROUND_AFTER_MAX,
    "get_length_max_utf16": GET_LENGTH_MAX_UTF16,
}


class HistoryBrokerAdapter:
    """Typed, owner-scoped history surface for managed callers."""

    def __init__(self, archive: Optional[ConversationArchive] = None) -> None:
        self._archive = archive

    @property
    def archive(self) -> ConversationArchive:
        if self._archive is None:
            self._archive = get_conversation_archive()
        return self._archive

    def _require_identity(
        self,
        binding: Mapping[str, Any],
    ) -> tuple[str, str]:
        owner = str(binding.get("owner") or "").strip()
        chat_id = str(binding.get("chat_id") or binding.get("stableChatID") or "").strip()
        if not owner:
            raise ArchiveUnavailableError(message="owner_required", retry_after_ms=0)
        return owner, chat_id

    def history_search(
        self,
        binding: Mapping[str, Any],
        *,
        query: str,
        scope: str = "chat",
        chat_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        part_types: Optional[Sequence[str]] = None,
        tool_name: Optional[str] = None,
        time_after: Optional[int] = None,
        time_before: Optional[int] = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        try:
            owner, bound_chat = self._require_identity(binding)
            # Callers may narrow to a chat only when it is their own binding.
            target_chat = chat_id or bound_chat
            if scope == "global":
                # Same-owner global: allowed, never cross-account.
                result = self.archive.search(
                    owner=owner,
                    query=query,
                    scope="global",
                    chat_id=chat_id or None,
                    actor_id=actor_id,
                    part_types=part_types,
                    tool_name=tool_name,
                    time_after=time_after,
                    time_before=time_before,
                    limit=limit,
                )
            else:
                if not target_chat:
                    return {"ok": False, "error": "chat_required", "hits": []}
                result = self.archive.search(
                    owner=owner,
                    query=query,
                    scope="chat",
                    chat_id=target_chat,
                    actor_id=actor_id,
                    part_types=part_types,
                    tool_name=tool_name,
                    time_after=time_after,
                    time_before=time_before,
                    limit=limit,
                )
            result["schema_version"] = HISTORY_SCHEMA_VERSION
            result["bounds"] = BOUNDS
            return result
        except ArchiveUnavailableError as exc:
            if exc.message in {"owner_required", "chat_required"}:
                return {"ok": False, "error": exc.message, "hits": []}
            return exc.as_result()
        except Exception as exc:  # pragma: no cover - defensive
            return ArchiveUnavailableError(message=str(exc)).as_result()

    def history_around(
        self,
        binding: Mapping[str, Any],
        *,
        anchor_message_id: str,
        before: int = 5,
        after: int = 5,
        actor_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            owner, chat_id = self._require_identity(binding)
            if not chat_id:
                return {"ok": False, "error": "chat_required", "messages": []}
            result = self.archive.around(
                owner=owner,
                chat_id=chat_id,
                anchor_message_id=anchor_message_id,
                before=before,
                after=after,
                actor_id=actor_id or "main",
            )
            result["schema_version"] = HISTORY_SCHEMA_VERSION
            result["bounds"] = BOUNDS
            return result
        except ArchiveUnavailableError as exc:
            if exc.message in {"owner_required", "chat_required"}:
                return {"ok": False, "error": exc.message, "messages": []}
            return exc.as_result()
        except Exception as exc:  # pragma: no cover - defensive
            return ArchiveUnavailableError(message=str(exc)).as_result()

    def history_get(
        self,
        binding: Mapping[str, Any],
        *,
        message_id: str,
        part_id: str,
        actor_id: Optional[str] = None,
        revision: Optional[int] = None,
        length: Optional[int] = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        try:
            owner, chat_id = self._require_identity(binding)
            if not chat_id:
                return {"ok": False, "error": "chat_required"}
            result = self.archive.get_part(
                owner=owner,
                chat_id=chat_id,
                actor_id=actor_id or "main",
                message_id=message_id,
                part_id=part_id,
                revision=revision,
                length=length,
                offset=offset,
            )
            result["schema_version"] = HISTORY_SCHEMA_VERSION
            result["bounds"] = BOUNDS
            return result
        except ArchiveUnavailableError as exc:
            if exc.message in {"owner_required", "chat_required"}:
                return {"ok": False, "error": exc.message}
            return exc.as_result()
        except Exception as exc:  # pragma: no cover - defensive
            return ArchiveUnavailableError(message=str(exc)).as_result()

    def history_media(
        self,
        binding: Mapping[str, Any],
        *,
        asset_id: str,
        message_id: Optional[str] = None,
        part_id: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            owner, chat_id = self._require_identity(binding)
            result = self.archive.get_media(
                owner=owner,
                asset_id=asset_id,
                chat_id=chat_id or None,
                message_id=message_id,
                part_id=part_id,
            )
            result["schema_version"] = HISTORY_SCHEMA_VERSION
            return result
        except ArchiveUnavailableError as exc:
            return exc.as_result()
        except Exception as exc:  # pragma: no cover - defensive
            return ArchiveUnavailableError(message=str(exc)).as_result()

    def history_append_parts(
        self,
        binding: Mapping[str, Any],
        parts: Sequence[SourcePart | Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Archive full ordered parts. Memory admission stays separately gated."""
        try:
            owner, chat_id = self._require_identity(binding)
            normalized = []
            for part in parts:
                if isinstance(part, SourcePart):
                    if ((part.owner and part.owner != owner) or (part.chat_id and part.chat_id != chat_id)):
                        raise ArchiveUnavailableError(message="history_scope_mismatch", retry_after_ms=0)
                    normalized.append(replace(part, owner=owner, chat_id=chat_id))
                    continue
                data = dict(part)
                if data.get("owner") not in (None, "", owner) or data.get("chat_id") not in (None, "", chat_id):
                    raise ArchiveUnavailableError(message="history_scope_mismatch", retry_after_ms=0)
                data["owner"] = owner
                data["chat_id"] = chat_id
                normalized.append(data)
            return self.archive.append_parts(normalized)
        except ArchiveUnavailableError as exc:
            return exc.as_result()
        except Exception as exc:  # pragma: no cover - defensive
            return ArchiveUnavailableError(message=str(exc)).as_result()


def history_tool_surface() -> dict[str, Any]:
    """JSON description of the typed history surface for broker consumers."""
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "methods": {
            "history.search": {
                "scope": ["chat", "global"],
                "limit_max": SEARCH_LIMIT_MAX,
            },
            "history.around": {
                "before_max": AROUND_BEFORE_MAX,
                "after_max": AROUND_AFTER_MAX,
            },
            "history.get": {
                "length_max_utf16": GET_LENGTH_MAX_UTF16,
                "reads": "canonical part, never FTS preview",
            },
            "history.media": {
                "mode": "explicit single-attachment",
                "policy": "owned asset ids only; no automatic file:// or network dereference",
            },
        },
    }
