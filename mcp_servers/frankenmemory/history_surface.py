"""Shared full-part history surface for the Frankenmemory typed surface.

Extends the existing ``mcp_servers/frankenmemory`` typed surface with the
conversation-archive history methods. No new daemon. Typed surface only:
schema-shaped helpers that the broker/host already authenticate and scope.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.openclank.conversation_archive import (
    AROUND_AFTER_MAX,
    AROUND_BEFORE_MAX,
    GET_LENGTH_MAX_UTF16,
    SEARCH_LIMIT_MAX,
    SourcePart,
    get_conversation_archive,
)
from src.openclank.history_broker_adapter import (
    HISTORY_SCHEMA_VERSION,
    HistoryBrokerAdapter,
    history_tool_surface,
)

__all__ = [
    "AROUND_AFTER_MAX",
    "AROUND_BEFORE_MAX",
    "GET_LENGTH_MAX_UTF16",
    "HISTORY_SCHEMA_VERSION",
    "HistoryBrokerAdapter",
    "SEARCH_LIMIT_MAX",
    "SourcePart",
    "get_conversation_archive",
    "history_tool_surface",
    "history_search",
    "history_around",
    "history_get",
    "history_media",
    "history_append_parts",
]


def history_search(binding: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return HistoryBrokerAdapter().history_search(binding, **kwargs)


def history_around(binding: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return HistoryBrokerAdapter().history_around(binding, **kwargs)


def history_get(binding: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return HistoryBrokerAdapter().history_get(binding, **kwargs)


def history_media(binding: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    return HistoryBrokerAdapter().history_media(binding, **kwargs)


def history_append_parts(
    binding: Mapping[str, Any],
    parts: Sequence[SourcePart | Mapping[str, Any]],
) -> dict[str, Any]:
    return HistoryBrokerAdapter().history_append_parts(binding, parts)
