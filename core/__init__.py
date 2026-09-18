# core/__init__.py
"""Side-effect-free compatibility facade for Open Clank's core modules.

Submodule imports such as ``core.atomic_io`` are used by the pre-application
engine/migration bootstrap.  Importing the package must therefore not eagerly
load the database, provider clients, or application session manager.  Historic
``core.<name>`` exports remain available lazily for callers that still use them.
"""

from __future__ import annotations

from importlib import import_module


_LAZY_EXPORTS = {
    "llm_call": ("src.llm_core", "llm_call"),
    "llm_call_async": ("src.llm_core", "llm_call_async"),
    "stream_llm": ("src.llm_core", "stream_llm"),
    "list_model_ids": ("src.llm_core", "list_model_ids"),
    "normalize_model_id": ("src.llm_core", "normalize_model_id"),
    "LLMConfig": ("src.llm_core", "LLMConfig"),
    "AuthManager": ("core.auth", "AuthManager"),
    "SecurityHeadersMiddleware": ("core.middleware", "SecurityHeadersMiddleware"),
    "SessionNotFoundError": ("core.exceptions", "SessionNotFoundError"),
    "InvalidFileUploadError": ("core.exceptions", "InvalidFileUploadError"),
    "LLMServiceError": ("core.exceptions", "LLMServiceError"),
    "WebSearchError": ("core.exceptions", "WebSearchError"),
    "Session": ("core.models", "Session"),
    "ChatMessage": ("core.models", "ChatMessage"),
    "SessionManager": ("core.session_manager", "SessionManager"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        value = getattr(import_module(target[0]), target[1])
        globals()[name] = value
        return value
    # The old facade wildcard-imported constants. Preserve direct attribute
    # compatibility without paying the import cost during pre-app submodule
    # loading.
    constants = import_module("core.constants")
    try:
        value = getattr(constants, name)
    except AttributeError as exc:
        raise AttributeError(f"module 'core' has no attribute {name!r}") from exc
    globals()[name] = value
    return value

__all__ = [
    # LLM
    "llm_call",
    "llm_call_async",
    "stream_llm",
    "list_model_ids",
    "normalize_model_id",
    "LLMConfig",
    # Auth
    "AuthManager",
    # Middleware
    "SecurityHeadersMiddleware",
    # Exceptions
    "SessionNotFoundError",
    "InvalidFileUploadError",
    "LLMServiceError",
    "WebSearchError",
    # Models
    "Session",
    "ChatMessage",
    "SessionManager",
]
