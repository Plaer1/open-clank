"""Full-part history access for Frankenmemory/Clank shared retrieval.

Exposes the shared archive surface (search/around/get/media) so callers do not
depend on text-pair capture or an FTS preview. Owner-scoped; guessed IDs fail
closed. This module is retrieval only — it never admits memory.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from src.openclank.conversation_archive import SourcePart, get_conversation_archive
from src.openclank.history_broker_adapter import HistoryBrokerAdapter, history_tool_surface


class HistoryArchiveSurface:
    """Scoped full-part history reads over the conversation archive."""

    def __init__(self, adapter: Optional[HistoryBrokerAdapter] = None) -> None:
        self._adapter = adapter or HistoryBrokerAdapter()

    def search(self, binding: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self._adapter.history_search(binding, **kwargs)

    def around(self, binding: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self._adapter.history_around(binding, **kwargs)

    def get(self, binding: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self._adapter.history_get(binding, **kwargs)

    def media(self, binding: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self._adapter.history_media(binding, **kwargs)

    def append_parts(
        self,
        binding: Mapping[str, Any],
        parts: Sequence[SourcePart | Mapping[str, Any]],
    ) -> dict[str, Any]:
        return self._adapter.history_append_parts(binding, parts)


__all__ = [
    "HistoryArchiveSurface",
    "HistoryBrokerAdapter",
    "SourcePart",
    "get_conversation_archive",
    "history_tool_surface",
]
