"""Presentation-only F0 format capability types.

These values describe existing routes and hints. They do not authorize access,
assert a decoder has inspected bytes, or establish a content hash.
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, TypedDict


class FormatOperation(TypedDict):
    available: bool
    reason: str | None


class FormatCapabilityV1(TypedDict):
    version: Literal[1]
    formatId: str
    variant: str | None
    mimeType: str | None
    sniff: dict[str, str]
    adapter: dict[str, str]
    operations: dict[str, FormatOperation]
    limits: dict[str, int | None]
    readiness: dict[str, str | None]
    outputKinds: list[str]
    resourceRef: str
    providerRevision: dict[str, Any] | None
    contentSHA256: str | None
    decoderGeneration: str | None
    policyGeneration: int


class FormatAdapter(Protocol):
    """Future finite adapters receive authorized input outside this descriptor."""

    adapter_id: str
    adapter_version: str
