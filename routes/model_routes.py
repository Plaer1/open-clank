# routes/model_routes.py
"""Routes for model and provider management."""
import asyncio
import os
import re
import uuid
import json
import hashlib
import ipaddress
import socket
import time as _time
import logging
import httpx
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Iterable
from urllib.parse import urlparse, urlunparse
from fastapi import APIRouter, HTTPException, Form, Query, Body, Request, Response
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse
from core.database import (
    ModelEndpoint,
    ModelShare,
    ModelShareSubscription,
    Session as DbSession,
    SessionLocal,
    utcnow_naive,
)
from core.log_safety import redact_url as _redact_url_for_log
from core.middleware import require_admin
from core.mimo_model_prefs import (
    owner_hidden_mimo_model_ids as _owner_hidden_mimo_model_ids,
    set_owner_hidden_mimo_models as _set_owner_hidden_mimo_models,
)
from src.constants import COOKBOOK_STATE_FILE
from src.llm_core import _detect_provider, _host_match, ANTHROPIC_MODELS
from src.tls_overrides import llm_verify
from src.settings import load_settings as _load_settings, save_settings as _save_settings
from src.endpoint_resolver import (
    DIRECT_ENDPOINT_PROVIDER_PREFIX,
    normalize_base as _normalize_base,
    canonical_endpoint_base,
    matching_endpoint,
    build_chat_url,
    build_models_url,
    build_headers,
    _endpoint_enabled_models,
)
from src.model_catalog import build_model_catalog
from src.chatgpt_subscription import is_chatgpt_subscription_base
from src.auth_helpers import effective_user, owner_filter
from src.openclank.mimo_projection import public_native_model_id
from src.openclank.chat_routing import (
    ChatRouteUnavailable,
    MANAGED_ENGINE_PUBLIC_URL,
    group_chat_routes,
    list_chat_routes,
    normalized_provider_owner,
)
from src.openclank.provider_store import (
    ProviderStore,
    ProviderStoreError,
    provider_family_display_name,
)
from src.model_shares import SharedModelAccess, own_native_auth, shared_endpoint_id

logger = logging.getLogger(__name__)

_MODEL_CATALOG_REVISION = 0
_MODEL_CATALOG_OWNER_REVISIONS: Dict[str, int] = {}


class ModelShareUpdate(BaseModel):
    endpoint_id: str
    model_id: str
    shared: bool
    recipients: list[str] = Field(default_factory=list, max_length=500)


class ModelShareSubscriptionUpdate(BaseModel):
    enabled: bool


def model_catalogue_revision(owner: str | None = None) -> tuple[int, int]:
    """Return the global and owner-local catalogue revision."""
    key = (owner or "").strip().lower()
    return _MODEL_CATALOG_REVISION, _MODEL_CATALOG_OWNER_REVISIONS.get(key, 0)


def invalidate_model_catalogue_revision(owner: str | None = None) -> int:
    """Invalidate all catalogues, or only one owner's catalogue."""
    global _MODEL_CATALOG_REVISION
    if owner is None:
        _MODEL_CATALOG_REVISION += 1
        return _MODEL_CATALOG_REVISION
    key = (owner or "").strip().lower()
    _MODEL_CATALOG_OWNER_REVISIONS[key] = (
        _MODEL_CATALOG_OWNER_REVISIONS.get(key, 0) + 1
    )
    return _MODEL_CATALOG_OWNER_REVISIONS[key]

_SPEECH_ENDPOINT_SETTINGS = (
    ("tts_provider", "tts_model", "tts-1", "Text to Speech"),
    ("stt_provider", "stt_model", "base", "Speech to Text"),
)

_ENDPOINT_SETTING_FIELDS = {
    "default_endpoint_id":  ("default_model",  "Default Model"),
    "utility_endpoint_id":  ("utility_model",   "Utility Model"),
    "memory_endpoint_id":   ("memory_model",    "Memory Model"),
    "research_endpoint_id": ("research_model",  "Deep Research"),
    "task_endpoint_id":     ("task_model",       "Background Tasks"),
}

_ENDPOINT_FALLBACK_FIELDS = {
    "default_model_fallbacks": "Default Model Fallbacks",
    "utility_model_fallbacks": "Utility Model Fallbacks",
    "memory_model_fallbacks": "Memory Model Fallbacks",
    "vision_model_fallbacks":  "Vision Model Fallbacks",
}


def _catalog_entitlement(base_url: str, model_ids: list[str]) -> Optional[bool]:
    """Mark live ChatGPT subscription discovery as entitled, not universal.

    Other OpenAI-compatible endpoints do not expose subscription entitlement
    semantics through this route, so their value remains unknown (``None``).
    """
    if is_chatgpt_subscription_base(base_url):
        return bool(model_ids)
    return None


def _speech_settings_using_endpoint(settings: dict, ep_id: str) -> list:
    """Return speech settings that reference a model endpoint."""
    endpoint_ref = f"endpoint:{ep_id}"
    return [
        label
        for provider_key, _, _, label in _SPEECH_ENDPOINT_SETTINGS
        if (settings.get(provider_key) or "") == endpoint_ref
    ]


def _clear_speech_settings_for_endpoint(settings: dict, ep_id: str) -> list:
    """Reset speech settings that reference a model endpoint."""
    endpoint_ref = f"endpoint:{ep_id}"
    cleared = []
    for provider_key, model_key, default_model, label in _SPEECH_ENDPOINT_SETTINGS:
        if (settings.get(provider_key) or "") == endpoint_ref:
            settings[provider_key] = "disabled"
            settings[model_key] = default_model
            cleared.append(label)
    return cleared


def _endpoint_settings_using_endpoint(settings: dict, ep_id: str, *, include_speech: bool = False) -> list:
    """Return labels for settings and fallback chains that reference an endpoint."""
    affected = []
    for ep_key, (_, label) in _ENDPOINT_SETTING_FIELDS.items():
        if (settings.get(ep_key) or "") == ep_id:
            affected.append(label)
    for fallback_key, label in _ENDPOINT_FALLBACK_FIELDS.items():
        chain = settings.get(fallback_key) or []
        if any(isinstance(entry, dict) and (entry.get("endpoint_id") or "") == ep_id for entry in chain):
            affected.append(label)
    if include_speech:
        affected.extend(_speech_settings_using_endpoint(settings, ep_id))
    return affected


def _clear_endpoint_settings_for_endpoint(settings: dict, ep_id: str, *, include_speech: bool = False) -> list:
    """Remove an endpoint from direct settings and model fallback chains."""
    cleared = []
    for ep_key, (model_key, label) in _ENDPOINT_SETTING_FIELDS.items():
        if (settings.get(ep_key) or "") == ep_id:
            settings[ep_key] = ""
            settings[model_key] = ""
            cleared.append(label)
    for fallback_key, label in _ENDPOINT_FALLBACK_FIELDS.items():
        chain = settings.get(fallback_key)
        if not isinstance(chain, list):
            continue
        kept = [
            entry for entry in chain
            if not (isinstance(entry, dict) and (entry.get("endpoint_id") or "") == ep_id)
        ]
        if len(kept) != len(chain):
            settings[fallback_key] = kept
            cleared.append(label)
    if include_speech:
        cleared.extend(_clear_speech_settings_for_endpoint(settings, ep_id))
    return cleared


_COOKBOOK_ACTIVE_SERVE_STATUSES = {
    "starting", "loading", "ready", "running", "restarting",
}


def _active_cookbook_endpoint_ids() -> set[str]:
    """Endpoint IDs owned by active Cookbook serve tasks.

    Cookbook auto-registers endpoints with ids like ``local-*``. Those rows are
    managed lifecycle state, not durable user configuration. If a tmux stream is
    stopped or an old task lingers, the row must stop participating in model
    selection and defaults.
    """
    try:
        if not os.path.exists(COOKBOOK_STATE_FILE):
            return set()
        with open(COOKBOOK_STATE_FILE, "r", encoding="utf-8") as fh:
            raw = fh.read()
        state = json.loads(raw)
    except Exception:
        return set()
    out: set[str] = set()
    for task in state.get("tasks") or []:
        if not isinstance(task, dict) or task.get("type") != "serve":
            continue
        if str(task.get("status") or "").lower() not in _COOKBOOK_ACTIVE_SERVE_STATUSES:
            continue
        ep_id = task.get("_endpointId") or task.get("endpointId") or task.get("endpoint_id")
        if ep_id:
            out.add(str(ep_id))
    return out


def _disable_stale_cookbook_local_endpoints(db, owner: str = "") -> int:
    """Disable this owner's cookbook endpoints whose serve task ended."""
    active_ids = _active_cookbook_endpoint_ids()
    if not active_ids:
        return 0
    stale_query = (
        db.query(ModelEndpoint)
        .filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        .filter(ModelEndpoint.id.like("local-%"))
        .filter(~ModelEndpoint.id.in_(active_ids))
    )
    stale = owner_filter(
        stale_query, ModelEndpoint, owner, include_shared=False
    ).all()
    if not stale:
        return 0
    if owner:
        from routes.prefs_routes import _load_for_user, _save_for_user

        settings = _load_for_user(owner)
        def save_settings(value):
            _save_for_user(owner, value)
    else:
        settings = _load_settings()
        save_settings = _save_settings
    touched_settings = False
    for ep in stale:
        ep.is_enabled = False
        ep.model_refresh_mode = "disabled"
        if _clear_endpoint_settings_for_endpoint(settings, ep.id):
            touched_settings = True
        logger.info("Disabled stale Cookbook endpoint %s (%s @ %s)", ep.id, ep.name, ep.base_url)
    if touched_settings:
        save_settings(settings)
    db.commit()
    return len(stale)


def _clear_user_pref_endpoint_refs(all_prefs: dict, ep_id: str) -> int:
    """Remove endpoint references from scoped or legacy-flat user preferences."""
    if not isinstance(all_prefs, dict):
        return 0
    users = all_prefs.get("_users")
    pref_sets = users.values() if isinstance(users, dict) else [all_prefs]
    cleared_users = 0
    for prefs in pref_sets:
        if isinstance(prefs, dict) and _clear_endpoint_settings_for_endpoint(prefs, ep_id):
            cleared_users += 1
    return cleared_users


def _endpoint_visible_model_ids(ep: Any) -> List[str]:
    """Known visible model ids for an endpoint, including pinned/manual ids."""
    if ep is None:
        return []
    return _visible_models(
        getattr(ep, "cached_models", None),
        getattr(ep, "hidden_models", None),
        getattr(ep, "pinned_models", None),
    )


def _default_endpoint_needs_assignment(
    current_default_id: str,
    enabled_endpoint_ids,
    *,
    current_default_endpoint: Any = None,
    current_default_model: str = "",
) -> bool:
    """Whether the global default chat endpoint should be (re)assigned.

    True when nothing is configured yet, or the configured default no longer
    resolves to an enabled endpoint (e.g. the user disabled it). Without the
    second case, adding a new endpoint after disabling the previous default
    leaves `default_endpoint_id` pointing at the disabled endpoint, so features
    that read the raw setting (Memory → Tidy) fail with "No default model
    configured" even though an enabled endpoint exists. See #3586.
    """
    if not current_default_id:
        return True
    if current_default_id not in enabled_endpoint_ids:
        return True
    if current_default_endpoint is None:
        return False
    if not (current_default_model or "").strip():
        return True
    visible = _endpoint_visible_model_ids(current_default_endpoint)
    return bool(visible and current_default_model not in visible)


def _stable_endpoint_choice(endpoints, preferred_model: str = ""):
    """Choose an owned endpoint deterministically, preferring model intent."""
    candidates = [
        endpoint
        for endpoint in endpoints
        if bool(getattr(endpoint, "is_enabled", True))
    ]
    candidates.sort(
        key=lambda endpoint: (
            str(getattr(endpoint, "name", "") or "").casefold(),
            str(getattr(endpoint, "id", "") or ""),
        )
    )
    if preferred_model:
        for endpoint in candidates:
            if preferred_model in _endpoint_visible_model_ids(endpoint):
                return endpoint
    return candidates[0] if candidates else None


# Loopback hosts a user might type for a local model server (LM Studio,
# llama.cpp, vLLM, …). Inside Docker these point at the *container*, not the
# host the server actually runs on.
_ANY_BIND_HOSTS = {"0.0.0.0", "::"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", *_ANY_BIND_HOSTS}


def _docker_host_gateway_reachable() -> bool:
    """True when we run inside a container whose host is reachable via
    ``host.docker.internal`` (compose maps it to ``host-gateway``). Returns
    False on native installs and on container setups without the mapping, so
    the loopback rewrite below stays a no-op there."""
    in_container = os.path.exists("/.dockerenv")
    if not in_container:
        try:
            with open("/proc/1/cgroup", encoding="utf-8") as fh:
                in_container = any(t in fh.read() for t in ("docker", "containerd", "kubepods"))
        except OSError:
            in_container = False
    if not in_container:
        return False
    try:
        socket.getaddrinfo("host.docker.internal", None)
        return True
    except OSError:
        return False

def _container_loopback_reachable(base_url: str, timeout: float = 0.2) -> bool:
    """True when the requested loopback host:port is already reachable from
    inside the current container.

    This distinguishes "a model server running alongside Open Clank in the same
    container" from "a model server running on the Docker host". Only the
    latter should be rewritten to host.docker.internal.
    """
    try:
        parsed = urlparse(base_url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if host not in _LOOPBACK_HOSTS or not port:
        return False
    probe_host = "::1" if host == "::1" else "127.0.0.1"
    family = socket.AF_INET6 if probe_host == "::1" else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((probe_host, port))
        return True
    except OSError:
        return False


def _rewrite_loopback_for_docker(base_url: str, *, container_local: bool = False) -> str:
    """Rewrite a loopback model-endpoint URL to ``host.docker.internal`` when
    running in Docker. A URL like ``http://localhost:1234/v1`` (the LM Studio
    default) otherwise targets the Open Clank container itself, so the probe gets
    a connection error and the endpoint is rejected with a misleading "No
    models found for that provider/key".

    Cookbook local serves are the opposite case: Open Clank started the model
    server inside the same container/process environment, so the saved endpoint
    must remain container-local. In that mode, normalize a bind address such as
    0.0.0.0 to a connectable loopback host, but do not jump to the Docker host.
    """
    try:
        parsed = urlparse(base_url)
    except Exception:
        return base_url
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return base_url
    if container_local:
        if host in _ANY_BIND_HOSTS:
            netloc = "127.0.0.1" + (f":{parsed.port}" if parsed.port else "")
            return urlunparse(parsed._replace(netloc=netloc))
        return base_url
    if host in _ANY_BIND_HOSTS and not _docker_host_gateway_reachable():
        netloc = "127.0.0.1" + (f":{parsed.port}" if parsed.port else "")
        return urlunparse(parsed._replace(netloc=netloc))
    if _container_loopback_reachable(base_url):
        return base_url
    if not _docker_host_gateway_reachable():
        return base_url
    netloc = "host.docker.internal" + (f":{parsed.port}" if parsed.port else "")
    return urlunparse(parsed._replace(netloc=netloc))


# ── Curated model lists per provider ──
# For cloud providers that return 100+ models, only show these by default.
# A model ID matches if it starts with or equals a curated entry.
_PROVIDER_CURATED = {
    "openai": [
        "gpt-5.2", "gpt-5.2-pro", "gpt-5", "gpt-5-pro", "gpt-5-mini", "gpt-5-nano",
        "gpt-4o", "gpt-4o-mini", "o3", "o4-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
        "gpt-image-1.5", "gpt-image-1", "dall-e-3", "tts-1", "whisper-1",
    ],
    "anthropic": [
        "claude-sonnet-4", "claude-opus-4", "claude-haiku-4",
        "claude-sonnet-4-5", "claude-haiku-3-5",
    ],
    "zai": [
        "glm-5", "glm-5.1", "glm-5v-turbo", "glm-4.7", "glm-4.7-flash",
        "glm-4.6", "glm-4.6v",
        "glm-4.5", "glm-4.5v", "glm-4.5-air", "glm-4.5-flash",
    ],
    "zai-coding": [
        "glm-5.1", "glm-5v-turbo", "glm-5-turbo", "glm-4.7", "glm-4.5-air",
    ],
    "kimi-code": [
        "kimi-for-coding",
    ],
    "deepseek": [
        "deepseek-chat", "deepseek-reasoner",
    ],
    "groq": [
        "openai/gpt-oss-120b", "openai/gpt-oss-20b",
        "groq/compound", "groq/compound-mini",
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "llama-4-scout-17b-16e-instruct",
        "llama-4-maverick-17b-128e-instruct",
    ],
    "mistral": [
        "mistral-large-latest", "mistral-medium-latest", "mistral-small-latest",
    ],
    "together": [
        "meta-llama/Llama-4-Scout-17B-16E-Instruct",
        "meta-llama/Llama-4-Maverick-17B-128E-Instruct",
        "deepseek-ai/DeepSeek-R1",
        "Qwen/Qwen2.5-72B-Instruct-Turbo",
    ],
    "fireworks": [
        "accounts/fireworks/models/llama4-scout-instruct-basic",
        "accounts/fireworks/models/llama4-maverick-instruct-basic",
        "accounts/fireworks/models/deepseek-r1",
    ],
    "google": [
        "gemini-3.5", "gemini-3.1", "gemini-3",
        "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash",
    ],
    "xai": [
        "grok-4.3", "grok-4", "grok-4-fast", "grok-3", "grok-3-fast",
    ],
}

# Map hostnames → curated-list keys for providers whose _detect_provider()
# returns a generic value (e.g. "openai") but deserve their own curated list.
# "openrouter" is a sentinel meaning "no curation — show all models as curated".
# Entries are matched by hostname equality or subdomain suffix (via _host_match),
# so e.g. "deepseek.com" covers api.deepseek.com without matching the substring
# inside an unrelated URL.
_HOST_TO_CURATED = (
    ("z.ai", "zai"),
    ("deepseek.com", "deepseek"),
    ("groq.com", "groq"),
    ("mistral.ai", "mistral"),
    ("together.xyz", "together"),
    ("together.ai", "together"),
    ("fireworks.ai", "fireworks"),
    ("googleapis.com", "google"),
    ("x.ai", "xai"),
    ("nvidia.com", "nvidia"),
    ("openrouter.ai", "openrouter"),
    ("ollama.com", "ollama"),
)


def _match_provider_curated(base_url: str, provider: str) -> str:
    """Return the curated-list key for a given endpoint.

    Checks path-based overrides first (for hosts serving multiple plans),
    then matches the base URL's hostname against known providers, and
    finally falls back to the raw provider string from _detect_provider().
    """
    # Path-based overrides for hosts that serve multiple curated lists.
    parsed = urlparse(base_url)
    if _host_match(base_url, "z.ai") and "/api/coding" in (parsed.path or ""):
        return "zai-coding"
    if _host_match(base_url, "kimi.com") and "/coding" in (parsed.path or ""):
        return "kimi-code"
    for domain, key in _HOST_TO_CURATED:
        if _host_match(base_url, domain):
            return key
    return provider


def _curate_models(model_ids, provider):
    """Partition model_ids into (curated, extra) based on provider's curated list.
    If no curated list exists for the provider, returns (model_ids, [])."""
    if provider == "openrouter":
        return model_ids, []
    curated_list = _PROVIDER_CURATED.get(provider)
    if not curated_list:
        return model_ids, []
    curated = []
    extra = []
    def _best_match_idx(mid):
        """Return index of the longest matching curated entry, or -1."""
        best_i, best_len = -1, 0
        for i, entry in enumerate(curated_list):
            if (mid == entry or mid.startswith(entry)) and len(entry) > best_len:
                best_i, best_len = i, len(entry)
        return best_i

    for mid in model_ids:
        if _best_match_idx(mid) >= 0:
            curated.append(mid)
        else:
            extra.append(mid)
    # Sort curated models by their priority order in the curated list
    curated.sort(key=lambda mid: (_best_match_idx(mid), mid))
    return curated, extra


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "1", "yes", "on")


_ENDPOINT_KINDS = {"auto", "local", "api", "proxy"}
_REFRESH_MODES = {"auto", "manual", "disabled"}


def _normalize_endpoint_kind(value: Any) -> str:
    kind = str(value or "auto").strip().lower()
    return kind if kind in _ENDPOINT_KINDS else "auto"


def _normalize_refresh_mode(value: Any, endpoint_kind: str = "auto") -> str:
    mode = str(value or "").strip().lower()
    kind = _normalize_endpoint_kind(endpoint_kind)
    if mode in ("manual", "disabled"):
        return mode
    if mode == "auto" and kind != "proxy":
        return "auto"
    # Proxies default to manual cached-first behavior. Normal local/API
    # endpoints keep automatic bounded refreshes.
    return "manual" if kind == "proxy" else "auto"


def _endpoint_kind(ep: Any) -> str:
    return _normalize_endpoint_kind(getattr(ep, "endpoint_kind", None))


def _endpoint_refresh_mode(ep: Any, endpoint_kind: str | None = None) -> str:
    return _normalize_endpoint_refresh_mode(
        getattr(ep, "model_refresh_mode", None),
        endpoint_kind or _endpoint_kind(ep),
        getattr(ep, "base_url", ""),
    )


def _endpoint_refresh_interval(ep: Any, category: str) -> float:
    raw = getattr(ep, "model_refresh_interval", None)
    try:
        val = int(raw) if raw is not None else 0
    except Exception:
        val = 0
    if val > 0:
        return float(max(30, val))
    return 60.0 if category == "local" else 3600.0


def _endpoint_refresh_timeout(ep: Any, category: str) -> float:
    raw = getattr(ep, "model_refresh_timeout", None)
    try:
        val = int(raw) if raw is not None else 0
    except Exception:
        val = 0
    if val > 0:
        return float(max(1, min(60, val)))
    # llama.cpp and other local OpenAI-compatible servers can block briefly
    # while warming/loading. A 2s local timeout makes working endpoints flicker
    # offline before /v1/models is ready.
    return 10.0 if category == "local" else 2.0


def _manual_refresh_timeout(ep: Any, category: str, requested: Any = None) -> float:
    """Timeout for explicit user-triggered model-list refreshes.

    Background refreshes stay short. A manual refresh is the one path where a
    large proxy may legitimately need 15-30s to aggregate its catalog.
    """
    requested_val = _parse_positive_int(requested, minimum=1, maximum=60)
    if requested_val is not None:
        return float(requested_val)
    stored = _parse_positive_int(getattr(ep, "model_refresh_timeout", None), minimum=1, maximum=60)
    if category == "local":
        return float(stored) if stored is not None else _endpoint_refresh_timeout(ep, category)
    return float(max(stored or 30, 30))


def _parse_model_list(raw: Any) -> List[str]:
    """Return a sanitized list of model ids from JSON/list/comma text."""
    if raw is None:
        return []
    value = raw
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                value = parsed
            else:
                value = re.split(r"[\n,]+", text)
        except Exception:
            value = re.split(r"[\n,]+", text)
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    for item in value:
        mid = str(item or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def _parse_positive_int(raw: Any, *, minimum: int = 1, maximum: int = 86400) -> Optional[int]:
    try:
        val = int(str(raw).strip())
    except Exception:
        return None
    if val < minimum:
        return None
    return min(val, maximum)


def _explicit_model_list_timeout(base_url: str, endpoint_kind: str = "auto", requested: Any = None) -> float:
    """Timeout for explicit user-triggered model-list fetches during setup."""
    requested_val = _parse_positive_int(requested, minimum=1, maximum=60)
    if requested_val is not None:
        return float(requested_val)
    kind = _normalize_endpoint_kind(endpoint_kind)
    category = _classify_endpoint(base_url, kind)
    if kind in ("api", "proxy") or category == "api":
        return 30.0
    return 15.0 if category == "local" else (3.0 if _is_ollama_base(base_url) else 2.0)


def _cached_model_ids(ep: Any) -> List[str]:
    return _parse_model_list(getattr(ep, "cached_models", None))


def _hidden_model_ids(ep: Any) -> set:
    return set(_parse_model_list(getattr(ep, "hidden_models", None)))


def _is_ollama_base(base_url: str) -> bool:
    try:
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        return parsed.port == 11434 or "ollama" in host
    except Exception:
        return "ollama" in (base_url or "").lower()


# Prefixes/substrings for models that are NOT chat-completions-capable
_NON_CHAT_PREFIXES = (
    "dall-e", "tts-", "whisper", "text-embedding", "embedding",
    "davinci", "babbage", "moderation", "omni-moderation",
    "sora", "gpt-image", "chatgpt-image",
    # embedding / retrieval / non-chat models (common across providers)
    "snowflake/arctic-embed", "nvidia/nv-embed", "embed",
)
_NON_CHAT_CONTAINS = (
    "-realtime", "-transcribe", "-tts", "content-safety", "-safety",
    "-reward", "nvclip",
    "kosmos", "fuyu", "deplot", "vila", "neva",
    "gliner", "riva", "-parse", "-embedqa", "-nemoretriever",
    "topic-control", "calibration",
    "ai-synthetic-video", "cosmos-reason2",
    "bge", "llama-guard",
)
_NON_CHAT_EXACT_PREFIXES = (
    "gpt-audio",  # gpt-audio, gpt-audio-mini etc. (not gpt-4o-audio-preview which is chat)
    "gpt-3.5-turbo-instruct",  # legacy OpenAI completions model
)


def _is_chat_model(model_id: str) -> bool:
    """Return True if the model ID looks like a chat/completions-capable model."""
    if not isinstance(model_id, str):
        # Non-compliant upstreams can return non-string IDs (e.g. int/None);
        # treat them as chat-capable rather than crashing on .lower().
        return True
    mid = model_id.lower()
    for prefix in _NON_CHAT_PREFIXES:
        if mid.startswith(prefix):
            return False
    for prefix in _NON_CHAT_EXACT_PREFIXES:
        if mid.startswith(prefix):
            return False
    for substr in _NON_CHAT_CONTAINS:
        if substr in mid:
            return False
    return True


# Direct-endpoint provider detection name → the provider id mimo uses for the
# same upstream. Everything not listed maps to itself.
_DIRECT_PROVIDER_TO_MIMO = {
    "chatgpt-subscription": "openai",
}

# Provider prefix → the model-family label users see. The upstream/operator
# provider is an admin detail; the family is the product identity.
_MIMO_FAMILY_NAMES = {
    "xiaomi": "MiMo",
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "moonshotai": "Moonshot",
    "z-ai": "Zhipu",
    "zai": "Zhipu",
}

_MIMO_PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "github-copilot": "GitHub Copilot",
    "google": "Google",
    "moonshotai": "Moonshot",
    "openai": "OpenAI",
    "xiaomi": "Xiaomi",
    "zai": "Z.AI",
}


def normalize_mimo_connection_id(endpoint_id: Any) -> Optional[str]:
    """Return the canonical native connection id, accepting legacy ``mimo``."""
    value = str(endpoint_id or "").strip().lower()
    if value in {"mimo", "mimo:auto"}:
        return "mimo:auto"
    if value.startswith("mimo:") and value[5:]:
        return value
    return None


def is_mimo_connection_id(endpoint_id: Any) -> bool:
    """Whether an endpoint id selects the native Open Clank agent runtime."""
    return normalize_mimo_connection_id(endpoint_id) is not None


def _mimo_endpoint_provider(ep_id) -> tuple[bool, str]:
    """Return whether an id is native MiMo and its optional provider key."""
    connection_id = normalize_mimo_connection_id(ep_id)
    if connection_id is None:
        return False, ""
    if connection_id == "mimo:auto":
        return True, ""
    return True, connection_id.split(":", 1)[1]


def _supervisor_available_models(supervisor, owner: str | None = None) -> list:
    if supervisor is None:
        return []
    try:
        try:
            return list(supervisor.available_models(owner=owner) or [])
        except TypeError:
            return list(supervisor.available_models() or [])
    except Exception:
        return []


def _connected_mimo_provider_ids(owner: str | None) -> set[str]:
    """Return provider accounts connected by this Open Clank user."""
    try:
        db = SessionLocal()
    except Exception as exc:
        logger.warning("Could not open provider connections for owner %r: %s", owner, exc)
        return set()
    try:
        return {
            _canonical_mimo_provider_id(provider_id)
            for provider_id in own_native_auth(db, owner or "")
            if _canonical_mimo_provider_id(provider_id)
        }
    except Exception as exc:
        logger.warning("Could not read provider connections for owner %r: %s", owner, exc)
        return set()
    finally:
        db.close()


def _public_mimo_model_ids(
    supervisor,
    owner: str | None = None,
    include_owner_hidden: bool = False,
) -> list[str]:
    """Return models backed by this user's connected provider accounts.

    Owner-hidden models (Settings toggles on provider accounts) are dropped
    unless ``include_owner_hidden`` — inventory counting needs the full set,
    display surfaces need the visible set."""
    models: list[str] = []
    seen: set[str] = set()
    connected = _connected_mimo_provider_ids(owner)
    hidden_by_owner = (
        set() if include_owner_hidden else _owner_hidden_mimo_model_ids(owner)
    )
    for item in _supervisor_available_models(supervisor, owner):
        if not isinstance(item, dict):
            continue
        model_id = item.get("modelId")
        if not isinstance(model_id, str) or not model_id:
            continue
        provider = model_id.split("/", 1)[0].lower()
        if provider.startswith(DIRECT_ENDPOINT_PROVIDER_PREFIX):
            continue
        model_id = public_native_model_id(model_id)
        if model_id in hidden_by_owner:
            continue
        public_provider = _canonical_mimo_provider_id(
            model_id.split("/", 1)[0] if "/" in model_id else ""
        )
        if model_id != "xiaomi/mimo-auto" and public_provider not in connected:
            continue
        if model_id not in seen:
            seen.add(model_id)
            models.append(model_id)
    return models


def _mimo_model_relations(
    supervisor,
    owner: str | None,
    model_ids: Iterable[str],
) -> dict[str, dict[str, str]]:
    """Keep ACP's explicit model/preset/variant hierarchy for visible models."""
    visible = set(model_ids)
    relations: dict[str, dict[str, str]] = {}
    for item in _supervisor_available_models(supervisor, owner):
        if not isinstance(item, dict):
            continue
        model_id = item.get("modelId")
        if not isinstance(model_id, str):
            continue
        model_id = public_native_model_id(model_id)
        if model_id not in visible or model_id in relations:
            continue
        relation: dict[str, str] = {}
        base_model_id = item.get("baseModelId")
        if isinstance(base_model_id, str) and base_model_id.strip():
            relation["base_model_id"] = public_native_model_id(base_model_id)
        for wire_key in ("preset", "variant"):
            value = item.get(wire_key)
            if isinstance(value, str) and value.strip():
                relation[wire_key] = value.strip()
        if relation:
            relations[model_id] = relation
    return relations


def ensure_owner_mimo_worker(supervisor, owner: str | None = None) -> bool:
    """Synchronously start a cold owner worker for sync catalogue routes."""
    if supervisor is None or _supervisor_available_models(supervisor, owner):
        return True
    starter = getattr(supervisor, "for_owner", None)
    if not callable(starter):
        return True
    runner = getattr(supervisor, "run_sync", None)
    if callable(runner):
        # MimoSupervisorPool owns a long-lived ACP event loop.  Sync FastAPI
        # routes execute in a thread, so marshal the coroutine back there;
        # never use asyncio.run() against the live supervisor.
        pending = starter(owner)
        try:
            runner(pending)
            return True
        except Exception as exc:
            close = getattr(pending, "close", None)
            if callable(close):
                close()
            logger.warning("Could not start model worker for owner %r: %s", owner, exc)
            return False
    try:
        import asyncio

        asyncio.run(starter(owner))
        return True
    except Exception as exc:
        logger.warning("Could not start model worker for owner %r: %s", owner, exc)
        return False


def mimo_connection_model_ids(
    supervisor,
    owner: str | None,
    endpoint_id: Any,
    include_owner_hidden: bool = False,
) -> list[str]:
    """Return chat models reachable through one canonical native connection."""
    is_mimo, provider = _mimo_endpoint_provider(endpoint_id)
    if not is_mimo:
        return []
    models = [
        model_id
        for model_id in _public_mimo_model_ids(
            supervisor, owner, include_owner_hidden=include_owner_hidden
        )
        if _is_chat_model(model_id)
    ]
    hidden = {
        item.strip()
        for item in os.environ.get("MIMO_HIDDEN_MODELS", "").split(",")
        if item.strip()
    }
    if hidden:
        models = [
            model_id
            for model_id in models
            if model_id not in hidden and model_id.rsplit("/", 1)[0] not in hidden
        ]
    if provider:
        models = [
            model_id
            for model_id in models
            if model_id.split("/", 1)[0] == provider
        ]
    covered = _covered_direct_models(models, owner)
    if covered:
        models = [model_id for model_id in models if model_id not in covered]
    return models


def _mimo_model_families(model_ids) -> dict[str, str]:
    families: dict[str, str] = {}
    for model_id in model_ids:
        if "/" not in model_id:
            continue
        prefix = model_id.split("/", 1)[0]
        families[model_id] = _MIMO_FAMILY_NAMES.get(prefix) or prefix.capitalize()
    return families


def _canonical_mimo_provider_id(provider: str) -> str:
    value = _DIRECT_PROVIDER_TO_MIMO.get(
        str(provider or "").strip().lower(),
        str(provider or "").strip().lower(),
    )
    if value.endswith("-coding"):
        value = value[: -len("-coding")]
    return {"z-ai": "zai"}.get(value, value)


def _direct_endpoint_matches_provider(ep, provider: str) -> bool:
    """Match one direct endpoint to a native provider without generic aliases."""
    base = str(getattr(ep, "base_url", "") or "")
    detected = _canonical_mimo_provider_id(_safe_detect_provider(base))
    curated = _canonical_mimo_provider_id(
        _match_provider_curated(base, detected)
    )
    wanted = _canonical_mimo_provider_id(provider)
    host = (urlparse(base).hostname or "").lower()
    compact_host = re.sub(r"[^a-z0-9]", "", host)
    compact_wanted = re.sub(r"[^a-z0-9]", "", wanted)
    if compact_wanted and compact_wanted in compact_host:
        return True
    if curated == wanted:
        return True
    # Provider detection calls most OpenAI-compatible APIs "openai". Do not
    # let that generic result claim a host that the URL curator identified as
    # a more specific provider such as DeepSeek or Z.AI.
    return detected == wanted and curated in {"", detected}


def _logical_model_id(model_id: str, provider: str) -> str:
    value = str(model_id or "").strip().casefold()
    prefix, separator, remainder = value.partition("/")
    if separator and _canonical_mimo_provider_id(prefix) == _canonical_mimo_provider_id(provider):
        return remainder
    return value


def _covered_direct_models(
    mimo_models: list[str],
    owner: str | None = None,
) -> dict[str, dict]:
    """Map only exact logical model overlaps to the direct endpoint that wins."""
    native_by_provider: dict[str, list[str]] = {}
    for model_id in mimo_models:
        provider, separator, _ = model_id.partition("/")
        if separator:
            native_by_provider.setdefault(provider, []).append(model_id)
    if not native_by_provider:
        return {}

    covered: dict[str, dict] = {}
    try:
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
            q = owner_filter(q, ModelEndpoint, owner or "", include_shared=False)
            endpoints = sorted(
                q.all(),
                key=lambda ep: (
                    str(getattr(ep, "name", "") or "").casefold(),
                    str(getattr(ep, "id", "") or ""),
                ),
            )
            for ep in endpoints:
                if (getattr(ep, "model_type", None) or "llm") != "llm":
                    continue
                direct_models = _endpoint_visible_model_ids(ep)
                if not direct_models:
                    continue
                for provider, native_models in native_by_provider.items():
                    if not _direct_endpoint_matches_provider(ep, provider):
                        continue
                    direct_keys = {
                        _logical_model_id(model_id, provider)
                        for model_id in direct_models
                    }
                    for model_id in native_models:
                        if _logical_model_id(model_id, provider) not in direct_keys:
                            continue
                        covered.setdefault(model_id, {
                            "endpoint_id": getattr(ep, "id", ""),
                            "endpoint_name": (
                                getattr(ep, "name", "")
                                or getattr(ep, "id", "")
                            ),
                        })
        finally:
            db.close()
    except Exception as exc:
        logger.debug("Direct-model coverage check failed: %s", exc)
        return {}
    return covered


def _mimo_provider_breakdown(supervisor, owner: str | None = None) -> list[dict]:
    """Per-provider integration state for Settings: what mimo has configured,
    what it actually serves, and exact overlaps served directly."""
    if supervisor is None:
        return []
    # Full inventory (including models the owner hid via Settings) so the
    # provider cards can show "K/N models enabled"; visibility filtering
    # happens per owner below.
    model_ids = _public_mimo_model_ids(supervisor, owner, include_owner_hidden=True)
    chat_model_ids = [
        model_id for model_id in model_ids if _is_chat_model(model_id)
    ]
    hidden = {
        item.strip()
        for item in os.environ.get("MIMO_HIDDEN_MODELS", "").split(",")
        if item.strip()
    }
    if hidden:
        chat_model_ids = [
            model_id
            for model_id in chat_model_ids
            if model_id not in hidden and model_id.rsplit("/", 1)[0] not in hidden
        ]
    owner_hidden = _owner_hidden_mimo_model_ids(owner)
    chat_model_set = set(chat_model_ids)
    visible_chat_models = [
        model_id for model_id in chat_model_ids if model_id not in owner_hidden
    ]
    visible_chat_set = set(visible_chat_models)
    covered = _covered_direct_models(visible_chat_models, owner)
    prefixes: dict[str, dict] = {}
    for model_id in model_ids:
        if "/" not in model_id:
            continue
        prefix = model_id.split("/", 1)[0]
        stats = prefixes.setdefault(
            prefix,
            {"models": 0, "chat_models": 0, "active_chat_models": 0, "hidden": 0},
        )
        stats["models"] += 1
        if model_id in chat_model_set:
            stats["chat_models"] += 1
            if model_id in owner_hidden:
                stats["hidden"] += 1
            elif model_id not in covered:
                stats["active_chat_models"] += 1
    families = _mimo_model_families([f"{prefix}/x" for prefix in prefixes])
    breakdown = []
    for prefix in sorted(prefixes):
        provider_models = [
            model_id
            for model_id in chat_model_ids
            if model_id.split("/", 1)[0] == prefix
        ]
        hidden_models = [
            model_id for model_id in provider_models if model_id in owner_hidden
        ]
        active_models = [
            model_id
            for model_id in provider_models
            if model_id not in covered and model_id not in owner_hidden
        ]
        covered_routes = [
            covered[model_id]
            for model_id in provider_models
            if model_id in covered and model_id in visible_chat_set
        ]
        active = prefixes[prefix]["active_chat_models"] > 0
        served_by = covered_routes[0] if covered_routes and not active else None
        breakdown.append({
            "id": prefix,
            "family": families.get(f"{prefix}/x") or prefix.capitalize(),
            "models": prefixes[prefix]["models"],
            "chat_models": prefixes[prefix]["chat_models"],
            "model_ids": active_models,
            "hidden_model_ids": hidden_models,
            "hidden_count": len(hidden_models),
            "active": active,
            "served_by": served_by,
        })
    return breakdown


def _mimo_catalog(supervisor, owner: str | None = None):
    """Return one filtered Mimo catalog for every model-list consumer."""
    if supervisor is None:
        return [], [], [], 0
    models = _public_mimo_model_ids(supervisor, owner)
    original_count = len(models)
    models = [model_id for model_id in models if _is_chat_model(model_id)]
    hidden = {item.strip() for item in os.environ.get("MIMO_HIDDEN_MODELS", "").split(",") if item.strip()}
    if hidden:
        models = [m for m in models if m not in hidden and m.rsplit("/", 1)[0] not in hidden]
    covered = _covered_direct_models(models, owner)
    if covered:
        models = [m for m in models if m not in covered]
    model_set = set(models)
    base = [m for m in models if "/" not in m or m.rsplit("/", 1)[0] not in model_set]
    variants = [m for m in models if "/" in m and m.rsplit("/", 1)[0] in model_set]
    return models, base, variants, original_count - len(models)


def _mimo_provider_catalogs(supervisor, owner: str | None = None) -> list[dict]:
    """Partition the native catalog into the real provider connections."""
    models, _base, _variants, _hidden_count = _mimo_catalog(supervisor, owner)
    relations = _mimo_model_relations(supervisor, owner, models)
    grouped: dict[str, list[str]] = {}
    for model_id in models:
        provider = model_id.split("/", 1)[0] if "/" in model_id else "auto"
        grouped.setdefault(_canonical_mimo_provider_id(provider), []).append(model_id)
    catalogs = []
    for provider, provider_models in grouped.items():
        model_set = set(provider_models)
        base = [
            model_id
            for model_id in provider_models
            if "/" not in model_id or model_id.rsplit("/", 1)[0] not in model_set
        ]
        variants = [model_id for model_id in provider_models if model_id not in base]
        catalogs.append({
            "id": provider,
            "name": _MIMO_PROVIDER_NAMES.get(
                provider,
                provider.replace("-", " ").title(),
            ),
            "models": provider_models,
            "base": base,
            "variants": variants,
            "displays": _mimo_display_names(base, variants),
            "relations": {
                model_id: relations[model_id]
                for model_id in provider_models
                if model_id in relations
            },
        })
    return sorted(catalogs, key=lambda item: item["name"].casefold())


def _mimo_display_names(base: list[str], variants: list[str]) -> dict[str, str]:
    """Keep Open Clank agent model names intact when ACP encodes reasoning effort in IDs."""
    displays = {model_id: model_id.split("/", 1)[-1] for model_id in base}
    for model_id in variants:
        model, _, effort = model_id.rpartition("/")
        displays[model_id] = f"{model.split('/', 1)[-1]} ({effort})"
    return displays


def _allowed_model_ids(request: Request, owner: str) -> Optional[frozenset[str]]:
    """Return None for unrestricted access, otherwise the exact visible IDs."""
    if not owner:
        return None
    auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    get_privileges = getattr(auth_manager, "get_privileges", None)
    if not get_privileges:
        return None
    privileges = get_privileges(owner) or {}
    if privileges.get("block_all_models"):
        return frozenset()
    raw = privileges.get("allowed_models")
    allowed = frozenset(str(item) for item in raw) if isinstance(raw, list) else frozenset()
    if privileges.get("allowed_models_restricted") or allowed:
        return allowed
    return None


def _filter_catalog_for_allowed_models(result: dict, allowed: Optional[frozenset[str]]) -> dict:
    if allowed is None:
        return result
    visible_items = []
    for item in result.get("items") or []:
        allowed_records = [
            record for record in (item.get("catalog") or [])
            if record.get("model_id") in allowed
            or record.get("provider_model_route_id") in allowed
            or record.get("_provider_model_route_id") in allowed
        ]
        allowed_public_ids = {
            str(record.get("model_id"))
            for record in allowed_records
            if record.get("model_id")
        }
        for ids_key, names_key in (
            ("models", "models_display"),
            ("models_extra", "models_extra_display"),
        ):
            ids = item.get(ids_key) or []
            names = item.get(names_key) or []
            pairs = [(model_id, names[index] if index < len(names) else model_id) for index, model_id in enumerate(ids)]
            item[ids_key] = [model_id for model_id, _ in pairs if model_id in allowed_public_ids]
            item[names_key] = [name for model_id, name in pairs if model_id in allowed_public_ids]
        item["catalog"] = allowed_records
        if item["catalog"] or item.get("models") or item.get("models_extra"):
            visible_items.append(item)
    result["items"] = visible_items
    return result


def _delete_orphaned_provider_auth(db, auth_id: Optional[str], exclude_ep_id: Optional[str] = None) -> bool:
    """Delete a ProviderAuthSession once no endpoint still references it."""
    if not auth_id:
        return False
    from core.database import ProviderAuthSession
    still_referenced = db.query(ModelEndpoint.id).filter(
        ModelEndpoint.provider_auth_id == auth_id,
        ModelEndpoint.id != exclude_ep_id,
    ).first()
    if still_referenced is not None:
        return False
    auth_row = db.query(ProviderAuthSession).filter(ProviderAuthSession.id == auth_id).first()
    if auth_row is None:
        return False
    db.delete(auth_row)
    return True


def _safe_detect_provider(base_url: str) -> str:
    """Best-effort provider detection that must not break endpoint probing."""
    try:
        return _detect_provider(base_url)
    except Exception as exc:
        logger.debug("Provider detection failed for %s: %s", base_url, exc)
        return ""


def _safe_build_models_url(base_url: str) -> str:
    """Build a /models URL without letting optional provider imports break probes."""
    try:
        return build_models_url(base_url)
    except ValueError:
        raise
    except Exception as exc:
        logger.debug("Model URL detection failed for %s: %s", base_url, exc)
        return f"{(base_url or '').rstrip('/')}/models"


def _safe_build_headers(api_key: Optional[str], base_url: str) -> dict:
    """Build auth headers without letting optional provider imports break probes."""
    try:
        return build_headers(api_key, base_url)
    except Exception as exc:
        logger.debug("Header detection failed for %s: %s", base_url, exc)
        return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _is_discovery_only_provider(provider: str) -> bool:
    return provider == "chatgpt-subscription"


def _resolve_probe_key(ep) -> Optional[str]:
    """API key/bearer to probe an endpoint with."""
    try:
        from src.endpoint_resolver import resolve_endpoint_runtime
        _base, key = resolve_endpoint_runtime(ep, owner=getattr(ep, "owner", None))
        return key
    except Exception as exc:
        logger.warning("Probe key resolution failed for %s: %s", getattr(ep, "id", "?"), exc)
        return None


def _probe_single_model(base: str, api_key: str, model_id: str, timeout: int = 10, with_tools: bool = False, *, owner: str, endpoint_id: str = "") -> dict:
    """Send a realistic completion request to a single model. Returns {status, latency_ms, error?}."""
    provider = _safe_detect_provider(base)
    if _is_discovery_only_provider(provider):
        return {"status": "ok", "latency_ms": 0, "skipped": True}
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Call the test tool with empty arguments. Do not answer in text." if with_tools else "Say OK"},
    ]
    # Simple tool definition to test tool support
    _test_tools = [{"type": "function", "function": {"name": "test", "description": "Test tool", "parameters": {"type": "object", "properties": {}}}}] if with_tools else None

    if provider == "anthropic":
        from src.llm_core import _normalize_anthropic_url, _build_anthropic_headers, _build_anthropic_payload
        target_url = _normalize_anthropic_url(base)
        auth_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        h = _build_anthropic_headers(auth_headers)
        payload = _build_anthropic_payload(model_id, messages, 0.0, 5)
        if _test_tools:
            payload["tools"] = [{"name": "test", "description": "Test tool", "input_schema": {"type": "object", "properties": {}}}]
            payload["tool_choice"] = {"type": "tool", "name": "test"}
    elif provider == "ollama":
        from src.llm_core import _build_ollama_payload
        target_url = build_chat_url(base)
        h = _safe_build_headers(api_key, base)
        h["Content-Type"] = "application/json"
        payload = _build_ollama_payload(model_id, messages, 0.0, 5, stream=False, tools=_test_tools)
    else:
        target_url = build_chat_url(base)
        h = _safe_build_headers(api_key, base)
        h["Content-Type"] = "application/json"
        from src.llm_core import _uses_max_completion_tokens, _restricts_temperature
        _max_key = "max_completion_tokens" if _uses_max_completion_tokens(model_id) else "max_tokens"
        # A forced tool call still needs enough output budget for reasoning
        # models to serialize the call. Five tokens produced a valid 200 with
        # no call on GLM-5.2, falsely leaving a tool-capable model unknown.
        payload = {"model": model_id, "messages": messages, _max_key: 64 if with_tools else 5}
        # Reasoning models (o1/o3/o4/gpt-5) reject an explicit temperature, so a
        # probe that hardcodes one falsely reports a working endpoint as failing.
        if not _restricts_temperature(model_id):
            payload["temperature"] = 0.0
        if _test_tools:
            payload["tools"] = _test_tools
            payload["tool_choice"] = {"type": "function", "function": {"name": "test"}}

    try:
        t0 = _time.time()
        from src.openclank.logging_capture_store import CAPTURE
        r = CAPTURE.observe_probe(owner=owner, provider_id=provider, endpoint_id=endpoint_id,
            model_id=model_id, url=target_url, headers=h, body=payload,
            send=lambda: httpx.post(target_url, headers=h, json=payload, timeout=timeout, verify=llm_verify()))
        latency = round((_time.time() - t0) * 1000)
        if r.is_success:
            if not with_tools:
                return {"status": "ok", "latency_ms": latency}
            try:
                body = r.json()
                calls = []
                if provider == "anthropic":
                    calls = [item for item in body.get("content", []) if item.get("type") == "tool_use"]
                else:
                    message = ((body.get("choices") or [{}])[0].get("message") or {})
                    calls = message.get("tool_calls") or []
                    if not calls and isinstance(body.get("message"), dict):
                        calls = body["message"].get("tool_calls") or []
                if any(
                    (call.get("name") or (call.get("function") or {}).get("name")) == "test"
                    for call in calls if isinstance(call, dict)
                ):
                    return {"status": "ok", "latency_ms": latency, "tool_support": True}
            except (ValueError, TypeError, AttributeError, IndexError):
                pass
            return {
                "status": "unknown",
                "latency_ms": latency,
                "tool_support": None,
                "error": "Model answered without the forced test tool call",
            }
        else:
            # Extract error detail from response body
            error_msg = f"HTTP {r.status_code}"
            try:
                body = r.json()
                if "error" in body:
                    err = body["error"]
                    if isinstance(err, dict):
                        error_msg = err.get("message", error_msg)[:120]
                    elif isinstance(err, str):
                        error_msg = err[:120]
            except Exception:
                pass
            unsupported = bool(
                with_tools
                and r.status_code in {400, 404, 405, 422}
                and re.search(r"tools?.*(?:not supported|unsupported|unknown)|tool_choice.*(?:not supported|unsupported|unknown)", error_msg, re.IGNORECASE)
            )
            return {
                "status": "unsupported" if unsupported else "unknown" if with_tools else "fail",
                "latency_ms": latency,
                "error": error_msg,
                **({"tool_support": False if unsupported else None} if with_tools else {}),
            }
    except httpx.TimeoutException:
        return {"status": "timeout", "latency_ms": timeout * 1000, "error": f"Timed out ({timeout}s)", **({"tool_support": None} if with_tools else {})}
    except Exception as e:
        return {"status": "unknown" if with_tools else "fail", "error": str(e)[:80], **({"tool_support": None} if with_tools else {})}


# Hostnames / IP prefixes that indicate a local endpoint
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _local_ip_literal(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in network for network in _PRIVATE_NETWORKS) or ip in _TAILSCALE_CGNAT


def _classify_endpoint(base_url: str, endpoint_kind: str = "auto") -> str:
    """Return 'local' if the endpoint URL points to a private/local address, else 'api'.
    Includes the Tailscale CGNAT range (100.64.0.0/10) so tailnet-hosted
    servers (e.g. Cookbook serve endpoints) get reachability-probed too."""
    kind = _normalize_endpoint_kind(endpoint_kind)
    if kind == "local":
        return "local"
    if kind in ("api", "proxy"):
        return "api"
    try:
        host = urlparse(base_url).hostname or ""
        if host in _LOCAL_HOSTS or _local_ip_literal(host):
            return "local"
    except Exception:
        pass
    return "api"


def _effective_endpoint_kind(ep: Any, base_url: str) -> str:
    """Return explicit kind, with a legacy proxy heuristic for keyed /v1 URLs."""
    from src.model_context import is_first_party_api_host

    kind = _endpoint_kind(ep)
    if kind != "auto":
        return kind
    if (
        getattr(ep, "api_key", None)
        and not _is_ollama_base(base_url)
        # A vendor's own API is not a multi-provider gateway, no matter how
        # its URL is shaped (api.deepseek.com/v1 was getting a PROXY badge,
        # manual refresh mode, and degraded context discovery).
        and not is_first_party_api_host(base_url)
    ):
        try:
            path = (urlparse(base_url).path or "").rstrip("/")
            if path.endswith("/v1") or "/openai" in path:
                return "proxy"
        except Exception:
            pass
    return "auto"


def _is_loading_model_response(resp: Any) -> bool:
    if getattr(resp, "status_code", None) != 503:
        return False
    try:
        body = resp.text or ""
    except Exception:
        body = ""
    return "loading model" in body.lower()



def _openai_model_ids(data: Any) -> List[str]:
    """Extract OpenAI-style model IDs.

    Accepts both standard ``{"data": [{"id": ...}]}`` responses and bare
    ``[{"id": ...}]`` lists returned by some OpenAI-compatible providers.
    Tolerates non-dict/non-list bodies and non-string IDs, returning only
    non-empty string IDs.
    """
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("data")
    else:
        items = None
    return [m["id"] for m in (items or [])
            if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]


def _ollama_model_names(data: Any) -> List[str]:
    """Extract native-Ollama model names (``{"models": [{"name"|"model": ...}]}``).

    Same tolerance as :func:`_openai_model_ids`: a non-dict body or non-string
    value is skipped rather than crashing, preserving name-then-model precedence.
    """
    items = data.get("models") if isinstance(data, dict) else None
    out: List[str] = []
    for m in (items or []):
        if not isinstance(m, dict):
            continue
        v = m.get("name") or m.get("model")
        if isinstance(v, str) and v:
            out.append(v)
    return out


def _mark_catalog_probe(
    diagnostics: Optional[Dict[str, Any]],
    status: str,
    http_status: Optional[int] = None,
) -> None:
    if diagnostics is None:
        return
    diagnostics.clear()
    diagnostics.update({
        "status": status,
        "http_status": http_status,
        "probed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    })


def _catalog_http_status(status: Optional[int]) -> str:
    if status == 401:
        return "auth_invalid"
    if status == 403:
        return "auth_forbidden"
    if status in {404, 405, 501}:
        return "unsupported"
    return "unavailable"


def _record_catalog_probe(endpoint: ModelEndpoint, diagnostics: Dict[str, Any]) -> None:
    endpoint.catalog_probe_status = diagnostics.get("status")
    endpoint.catalog_probe_http_status = diagnostics.get("http_status")
    raw_time = diagnostics.get("probed_at")
    try:
        endpoint.catalog_probed_at = datetime.fromisoformat(str(raw_time).removesuffix("Z"))
    except (TypeError, ValueError):
        endpoint.catalog_probed_at = utcnow_naive()


def _catalog_probe_payload(endpoint: ModelEndpoint) -> Dict[str, Any]:
    status = getattr(endpoint, "catalog_probe_status", None)
    actions = {
        "auth_missing": ["repair_credentials"],
        "auth_invalid": ["repair_credentials"],
        "auth_forbidden": ["repair_credentials"],
        "unavailable": ["retry_catalog_probe"],
        "loading": ["retry_catalog_probe"],
        "malformed": ["check_provider_compatibility", "retry_catalog_probe"],
        "unsupported": ["pin_models", "check_provider_compatibility"],
        "empty": ["pin_models", "retry_catalog_probe"],
    }.get(status, [])
    probed_at = getattr(endpoint, "catalog_probed_at", None)
    return {
        "status": status or "unknown",
        "http_status": getattr(endpoint, "catalog_probe_http_status", None),
        "probed_at": probed_at.isoformat() + "Z" if probed_at else None,
        "actions": actions,
    }


def _is_google_api_base(base_url: str) -> bool:
    try:
        return (urlparse(base_url).hostname or "").lower() == "generativelanguage.googleapis.com"
    except Exception:
        return False


def _normalize_endpoint_refresh_mode(value: Any, endpoint_kind: str = "auto", base_url: str = "") -> str:
    if not str(value or "").strip() and _is_google_api_base(base_url):
        return "manual"
    return _normalize_refresh_mode(value, endpoint_kind)


def _google_native_root(base_url: str) -> str:
    """Return the Gemini native API root for a Google endpoint.

    Chat calls may be configured against Google's OpenAI-compatible
    `/openai` path, but model catalog reads should use the native Models API
    so we get Google's current Model resource shape.
    """
    try:
        parsed = urlparse(base_url)
    except Exception:
        return "https://generativelanguage.googleapis.com/v1beta"
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/openai"):
        path = path[: -len("/openai")].rstrip("/")
    if not path:
        path = "/v1beta"
    return urlunparse(parsed._replace(path=path, query="", fragment="")).rstrip("/")


def _google_native_models_url(base_url: str) -> str:
    return _google_native_root(base_url) + "/models"


def _google_model_id_from_item(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    value = item.get("baseModelId") or item.get("name") or item.get("model") or ""
    return str(value or "").strip().removeprefix("models/")


def _google_model_supports_chat(item: Any) -> bool:
    """Return whether a native Google Model resource supports chat generation."""
    if not isinstance(item, dict):
        return False
    methods = item.get("supportedGenerationMethods")
    if not isinstance(methods, list):
        return False
    chat_methods = {"generateContent", "generateMessage", "generateText", "generateAnswer"}
    return any(method in chat_methods for method in methods)


def _probe_google_models(base_url: str, api_key: str = None, timeout: int = 5, page_size: int = 1000) -> List[str]:
    """Read Google's native paginated Models API.

    This intentionally returns only provider-reported model IDs. Capability
    mapping is handled by the model capability reader and must not infer from
    names here.
    """
    url = _google_native_models_url(base_url)
    try:
        page_size = min(max(int(page_size or 1000), 1), 1000)
    except Exception:
        page_size = 1000
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = api_key
    params: Dict[str, Any] = {"pageSize": page_size}
    models: List[str] = []
    seen = set()
    page_token = ""
    for _ in range(20):
        request_params = dict(params)
        if page_token:
            request_params["pageToken"] = page_token
        r = httpx.get(url, headers=headers, params=request_params, timeout=timeout, verify=llm_verify())
        r.raise_for_status()
        data = r.json()
        for item in data.get("models") or []:
            if not _google_model_supports_chat(item):
                continue
            model_id = _google_model_id_from_item(item)
            if model_id and model_id not in seen:
                seen.add(model_id)
                models.append(model_id)
        page_token = str(data.get("nextPageToken") or "").strip()
        if not page_token:
            break
    return models


def _probe_endpoint(
    base_url: str,
    api_key: str = None,
    timeout: int = 5,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Probe a base URL's /models endpoint and return list of model IDs.
    For Anthropic, queries their /v1/models API, falling back to hardcoded list."""
    _mark_catalog_probe(diagnostics, "unknown")
    from src.endpoint_resolver import resolve_url
    base = resolve_url(_normalize_base(base_url))
    provider = _safe_detect_provider(base)
    if provider == "chatgpt-subscription":
        from src.chatgpt_subscription import fetch_available_models
        if not api_key:
            _mark_catalog_probe(diagnostics, "auth_missing")
            return []
        try:
            models = fetch_available_models(api_key, timeout=timeout)
            _mark_catalog_probe(diagnostics, "ok" if models else "empty")
            return models
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            _mark_catalog_probe(diagnostics, _catalog_http_status(status), status)
            return []
        except Exception:
            _mark_catalog_probe(diagnostics, "unavailable")
            logger.warning("ChatGPT subscription model discovery failed")
            return []
    if _is_google_api_base(base):
        try:
            models = _probe_google_models(base, api_key, timeout=timeout)
            if models:
                _mark_catalog_probe(diagnostics, "ok")
                return models
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else None
            _mark_catalog_probe(diagnostics, _catalog_http_status(status), status)
            logger.warning(f"Google native models probe failed: HTTP {status or 'unknown'}")
        except Exception as e:
            _mark_catalog_probe(diagnostics, "unavailable")
            logger.warning(f"Google native models probe failed: {e}")
        else:
            _mark_catalog_probe(diagnostics, "empty")
        return []
    if provider == "anthropic":
        # Try Anthropic's /v1/models endpoint first
        url = _safe_build_models_url(base)
        headers = {"anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        try:
            r = httpx.get(url, headers=headers, timeout=timeout, verify=llm_verify())
            r.raise_for_status()
            data = r.json()
            models = _openai_model_ids(data)
            if models:
                _mark_catalog_probe(diagnostics, "ok", r.status_code)
                return models
            _mark_catalog_probe(diagnostics, "empty", r.status_code)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else None
            _mark_catalog_probe(diagnostics, _catalog_http_status(status), status)
            if api_key:
                logger.warning(f"Anthropic /v1/models failed with API key: HTTP {status or 'unknown'}")
                return []
            logger.warning(f"Anthropic /v1/models failed, using hardcoded list: {e}")
        except Exception as e:
            _mark_catalog_probe(
                diagnostics,
                "malformed" if isinstance(e, (ValueError, json.JSONDecodeError)) else "unavailable",
            )
            if api_key:
                logger.warning(f"Anthropic /v1/models failed with API key: {e}")
                return []
            logger.warning(f"Anthropic /v1/models failed, using hardcoded list: {e}")
        return list(ANTHROPIC_MODELS)
    url = _safe_build_models_url(base)
    headers = _safe_build_headers(api_key, base)
    try:
        r = httpx.get(url, headers=headers, timeout=timeout, verify=llm_verify())
        r.raise_for_status()
        data = r.json()
        # OpenAI format: {"data": [{"id": "model-name"}]}
        models = _openai_model_ids(data)
        # Ollama format: {"models": [{"name": "model-name"}]}
        if not models:
            models = _ollama_model_names(data)
        if models:
            # Z.AI coding plan omits some working models from /models;
            # append curated-only entries for that endpoint only.
            if _host_match(base, "z.ai") and "/api/coding" in (urlparse(base).path or ""):
                _ck = _match_provider_curated(base, None)
                for _e in _PROVIDER_CURATED.get(_ck, []):
                    if _e not in set(models) and not any(m.startswith(_e) for m in models):
                        models.append(_e)
            if _host_match(base, "kimi.com") and "/coding" in (urlparse(base).path or ""):
                _ck = _match_provider_curated(base, None)
                for _e in _PROVIDER_CURATED.get(_ck, []):
                    if _e not in set(models) and not any(m.startswith(_e) for m in models):
                        models.append(_e)
            _mark_catalog_probe(diagnostics, "ok", r.status_code)
            return [m for m in models if _is_chat_model(m)]
        _mark_catalog_probe(diagnostics, "empty", r.status_code)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else None
        _mark_catalog_probe(diagnostics, _catalog_http_status(status), status)
        if e.response is not None and _is_loading_model_response(e.response):
            logger.info("Endpoint still loading model at %s", _redact_url_for_log(url))
            return []
        if api_key:
            logger.warning("Failed to probe %s with API key: HTTP %s", _redact_url_for_log(url), status or "unknown")
            return []
        logger.warning("Failed to probe %s: %s", _redact_url_for_log(url), e)
    except Exception as e:
        _mark_catalog_probe(
            diagnostics,
            "malformed" if isinstance(e, (ValueError, json.JSONDecodeError)) else "unavailable",
        )
        if api_key:
            logger.warning("Failed to probe %s with API key: %s", _redact_url_for_log(url), e)
            return []
        logger.warning("Failed to probe %s: %s", _redact_url_for_log(url), e)

    # Older Ollama builds and some proxies expose native /api/tags even when
    # the OpenAI-compatible /v1/models path is unavailable.
    try:
        parsed = urlparse(base)
        if parsed.port == 11434 or "ollama" in (parsed.hostname or "").lower():
            root = base[:-3].rstrip("/") if base.endswith("/v1") else base
            r = httpx.get(root + "/api/tags", timeout=timeout, verify=llm_verify())
            r.raise_for_status()
            data = r.json()
            models = _ollama_model_names(data)
            if models:
                _mark_catalog_probe(diagnostics, "ok", r.status_code)
                return [m for m in models if _is_chat_model(m)]
    except Exception as e:
        logger.debug(f"Ollama /api/tags probe failed for {base}: {e}")
    # Fall back to curated list if the provider has a URL-based match (e.g. z.ai has no /models endpoint)
    curated_key = _match_provider_curated(base, None)
    fallback = _PROVIDER_CURATED.get(curated_key) if curated_key else None
    if fallback:
        logger.info(f"Using curated fallback for {curated_key}: {fallback}")
        return list(fallback)
    if diagnostics is not None and diagnostics.get("status") == "unknown":
        _mark_catalog_probe(diagnostics, "empty")
    return []


def _probe_endpoint_result(
    base_url: str,
    api_key: str = None,
    timeout: int = 5,
) -> tuple[List[str], Dict[str, Any]]:
    """Return models plus typed probe evidence without breaking legacy overrides."""
    diagnostics: Dict[str, Any] = {}
    try:
        models = _probe_endpoint(
            base_url,
            api_key,
            timeout=timeout,
            diagnostics=diagnostics,
        )
    except TypeError as exc:
        # Some installed integrations and test doubles override the historical
        # three-argument helper. Preserve that contract while the canonical
        # implementation records richer evidence.
        if "diagnostics" not in str(exc):
            raise
        models = _probe_endpoint(base_url, api_key, timeout=timeout)
        _mark_catalog_probe(diagnostics, "ok" if models else "empty")
    return models, diagnostics


def _ping_endpoint(base_url: str, api_key: str = None, timeout: float = 1.5) -> Dict[str, Any]:
    """Reachability probe that does not require installed/listed models."""
    from src.endpoint_resolver import resolve_url
    base = resolve_url(_normalize_base(base_url))
    headers = _safe_build_headers(api_key, base)

    # Ollama exposes /v1/models (OpenAI-compatible) AND native /api/version,
    # /api/tags. Probe native paths for Ollama-style endpoints, but avoid using
    # /models as a generic health check because large proxy catalogs can be slow.
    parsed_base = urlparse(base)
    looks_like_ollama = (
        parsed_base.port == 11434
        or "ollama" in (parsed_base.hostname or "").lower()
    )

    def _is_loading_model_response(r) -> bool:
        if getattr(r, "status_code", None) != 503:
            return False
        try:
            body = r.text or ""
        except Exception:
            body = ""
        return "loading model" in body.lower()

    def _result_from_response(r) -> Dict[str, Any]:
        if 300 <= r.status_code < 400:
            loc = r.headers.get("location", "")
            if loc.startswith("/login") or "/login" in loc:
                return {
                    "reachable": False,
                    "status_code": r.status_code,
                    "error": "That is Open Clank, not a model server. Use the Ollama URL, usually http://host.docker.internal:11434/v1 in Docker.",
                }
            return {"reachable": False, "status_code": r.status_code, "error": f"HTTP {r.status_code} redirect"}
        if 200 <= r.status_code < 300:
            return {
                "reachable": True,
                "status_code": r.status_code,
                "error": None,
            }
        if _is_loading_model_response(r):
            return {
                "reachable": True,
                "loading": True,
                "status_code": r.status_code,
                "error": "Loading model",
            }
        return {"reachable": False, "status_code": r.status_code, "error": f"HTTP {r.status_code}"}

    last_error: Optional[str] = None

    try:
        if looks_like_ollama:
            root = base
            for suffix in ("/v1", "/api"):
                if root.endswith(suffix):
                    root = root[: -len(suffix)].rstrip("/")
                    break
            for path in ("/api/version", "/api/tags"):
                try:
                    r = httpx.get(root + path, timeout=timeout, verify=llm_verify())
                    result = _result_from_response(r)
                    if result["reachable"]:
                        return result
                    last_error = result.get("error")
                except Exception as e:
                    last_error = str(e)[:120]
    except Exception:
        pass

    try:
        r = httpx.get(base, headers=headers, timeout=timeout, verify=llm_verify())
        result = _result_from_response(r)
        if result["reachable"]:
            return result
        sc = result.get("status_code") or 0
        if 400 <= sc < 500 and sc not in (401, 403):
            models_url = _safe_build_models_url(base)
            try:
                r2 = httpx.get(models_url, headers=headers,timeout=timeout, verify=llm_verify())
                result2 = _result_from_response(r2)
                if result2["reachable"]:
                    return result2
            except Exception:
                pass
        if sc:
            return result
        last_error = result.get("error") or last_error
    except Exception as e:
        last_error = str(e)[:120]

    return {"reachable": False, "status_code": None, "error": last_error}



def _model_endpoint_error_message(base_url: str, ping: Dict[str, Any] = None) -> str:
    """Return a provider-aware error message for failed endpoint probes.

    Surfaces the URL we actually probed and, when the endpoint looks like
    LM Studio (port 1234 or hostname match), adds a hint about loading a
    model and confirming the Developer Server is running. The user previously
    saw a generic "No models found for that provider/key" with no way to
    tell whether the URL was wrong, the server was down, or the server was
    reachable but had no model loaded (issue #25).
    """
    ping = ping or {}
    error = ping.get("error")
    from src.endpoint_resolver import build_models_url
    try:
        probed = build_models_url(base_url) or base_url
    except Exception:
        probed = base_url
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    is_ollama = parsed.port == 11434 or "ollama" in host or "ollama" in base_url.lower()
    is_lmstudio = (
        parsed.port == 1234
        or "lmstudio" in host
        or "lm-studio" in host
        or "lm_studio" in host
    )

    if is_lmstudio:
        parts = [
            "LM Studio is reachable, but no models were reported.",
            f"Probed {probed}.",
        ]
        if error:
            parts.append(f"Last probe error: {error}.")
        parts.append(
            "Open LM Studio, load at least one model, and confirm the "
            "Developer Server is running on port 1234."
        )
        parts.append(
            "Base URL should be http://localhost:1234/v1 (native) or "
            "http://host.docker.internal:1234/v1 (Docker)."
        )
        return " ".join(parts)

    if is_ollama:
        parts = ["No Ollama models found for that endpoint."]
        parts.append(f"Probed {probed}.")
        if error:
            parts.append(f"Last probe error: {error}.")
        parts.append("Check that Ollama is running and that the base URL is correct.")
        parts.append("For native/local installs, use http://localhost:11434/v1.")
        parts.append("For Docker, use http://host.docker.internal:11434/v1 when Ollama runs on the host.")
        parts.append("Run `ollama list` to confirm at least one model is installed.")
        return " ".join(parts)

    if error:
        return f"No models found for that provider/key. Probed {probed}. Last probe error: {error}."

    return f"No models found for that provider/key. Probed {probed}."


def _normalize_model_ids(value):
    """Coerce a model-ID input into a clean, ordered list of strings.

    Accepts a list, a JSON-encoded list string, or a comma/newline separated
    string (handy for form or backend API input). Trims whitespace, drops
    empty and non-string values, and de-duplicates preserving first-seen order.
    """
    if value is None:
        return []
    items = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        items = parsed if isinstance(parsed, list) else re.split(r"[,\n]", text)
    if not isinstance(items, list):
        return []
    out, seen = [], set()
    for item in items:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _merge_model_ids(*lists):
    """Concatenate model-ID lists, de-duplicating and preserving order."""
    out, seen = [], set()
    for ids in lists:
        for m in (ids or []):
            if not isinstance(m, str) or m in seen:
                continue
            seen.add(m)
            out.append(m)
    return out


def _is_mlx_deepseek_v4_repo_id(model_id: str) -> bool:
    m = str(model_id or "").lower()
    return "mlx-community/deepseek-v4" in m


def _is_mlx_deepseek_v4_shim_id(model_id: str) -> bool:
    m = str(model_id or "").lower()
    return "/.cache/odysseus/mlx-shims/deepseek-v4" in m


def _filter_mlx_deepseek_v4_repo_when_shimmed(model_ids):
    """Hide the broken MLX repo id when a launch-specific shim id is available.

    mlx_lm.server may advertise the original HF repo id even though generation
    only works through Open Clank' sanitized local shim. Keep the shim as the
    submitted model id and remove the raw repo id from the picker/default list.
    """
    ids = list(model_ids or [])
    has_shim = any(_is_mlx_deepseek_v4_shim_id(m) for m in ids)
    if not has_shim:
        return ids
    return [m for m in ids if not _is_mlx_deepseek_v4_repo_id(m)]


def _model_display_name(model_id: str) -> str:
    if _is_mlx_deepseek_v4_shim_id(model_id):
        return str(model_id or "").rstrip("/").split("/")[-1] or "DeepSeek-V4-Flash-4bit"
    return str(model_id or "").split("/")[-1]


async def revoke_shared_model_workers(
    request: Request,
    revocations: list[tuple[str, str, str]],
) -> None:
    """Stop only the recipient/share runtime partitions being revoked."""
    supervisor = getattr(getattr(request, "app", None), "state", None)
    supervisor = getattr(supervisor, "mimo_supervisor", None)
    revoke = getattr(supervisor, "revoke_shared_access", None)
    if not callable(revoke) or not revocations:
        return
    results = await asyncio.gather(
        *(
            revoke(recipient, share_id)
            for recipient, share_id, _model_id in set(revocations)
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("trusted-share worker revoke failed: %s", result)


def notify_shared_model_removals(
    revocations: list[tuple[str, str, str]],
    *,
    shared_by: str,
) -> None:
    """Queue recipient-scoped UI notices without exposing source credentials."""
    if not revocations:
        return
    from src.event_bus import get_task_scheduler

    scheduler = get_task_scheduler()
    add_notification = getattr(scheduler, "add_notification", None)
    if not callable(add_notification):
        return
    for recipient, share_id, model_id in set(revocations):
        model_name = _model_display_name(model_id) or "a model"
        add_notification(
            "Shared model removed",
            "success",
            task_id=f"model-share:{share_id}",
            owner=recipient,
            body=(
                f"{shared_by} stopped sharing {model_name} with you. "
                "Choose another model for any open chat that used it."
            ),
            kind="model_share",
        )


def _visible_models(cached_models, hidden_models, pinned_models=None):
    """Merge cached + pinned model IDs, then filter out hidden ones.

    Pinned IDs are admin-entered and may not appear in cached_models (e.g.
    cloud deployment IDs the provider does not list in /v1/models). Returns an
    ordered, de-duplicated list of visible IDs.
    """
    # Normalize each input so JSON strings, lists, comma/newline strings, and
    # malformed strings are all handled without raising.
    merged = _merge_model_ids(
        _normalize_model_ids(cached_models),
        _normalize_model_ids(pinned_models),
    )
    merged = _filter_mlx_deepseek_v4_repo_when_shimmed(merged)
    if not hidden_models:
        return merged
    hidden = set(_normalize_model_ids(hidden_models))
    return [m for m in merged if m not in hidden]


def _picker_requires_pinning(base_url: str, kind: str) -> bool:
    return _classify_endpoint(base_url, kind) == "api"


def _has_explicit_pinned_models(ep) -> bool:
    """Whether pinned_models was deliberately written for this endpoint.

    API endpoints use pinned_models as an allow-list. An explicit empty JSON
    list means "show no models"; it must not fall back to the old hidden-list
    migration behavior.
    """
    raw = getattr(ep, "pinned_models", None)
    return raw is not None and str(raw).strip() != ""


def _legacy_visible_api_models(ep) -> List[str]:
    """Return API models selected under the old hidden-list picker.

    Before API endpoints switched to an explicit allow-list, selected models
    were represented as cached_models minus hidden_models. Existing OpenRouter
    rows can therefore have many checked models and an empty pinned_models
    field. Treat that old state as the initial pinned list so settings and chat
    agree after upgrade.
    """
    return _visible_models(
        _cached_model_ids(ep),
        getattr(ep, "hidden_models", None),
        None,
    )


def _picker_models_for_endpoint(ep, base_url: str, kind: str):
    """Return model IDs that should appear in the picker for an endpoint.

    API providers expose remote inventory from /v1/models. Treat that cache as
    inventory, not approval: only manually pinned API models should appear in
    the picker. Local/self-hosted endpoints keep the older hide-list behavior.
    """
    pinned = _normalize_model_ids(getattr(ep, "pinned_models", None))
    if _picker_requires_pinning(base_url, kind):
        if not _has_explicit_pinned_models(ep):
            pinned = _legacy_visible_api_models(ep) if _hidden_model_ids(ep) else []
        return pinned, pinned
    return _visible_models(
        _cached_model_ids(ep),
        getattr(ep, "hidden_models", None),
        pinned,
    ), pinned


def _api_pin_seed(ep, cached_ids=None) -> Optional[List[str]]:
    """Seed that makes an untouched API row visible in chat by default.

    Vanilla Odysseus semantics are opt-out: every connected model appears in
    the chat picker until the owner hides it. API-class pickers enforce the
    allow-list ("cache is inventory, not approval"), so a row whose pins were
    never written renders nothing. Returns the chat-capable inventory as the
    seed, or None when the row must not be touched: no inventory, not
    API-class, pins already written (an explicit empty list means "show
    nothing"), a legacy hidden list carrying older owner intent, or an
    ownerless row while auth is configured (legacy/unclaimed data).
    """
    if cached_ids is None:
        cached_ids = _cached_model_ids(ep)
    if not cached_ids:
        return None
    if not (getattr(ep, "owner", None) or "").strip():
        return None
    base = _normalize_base(ep.base_url)
    kind = _effective_endpoint_kind(ep, base)
    if not _picker_requires_pinning(base, kind):
        return None
    if _has_explicit_pinned_models(ep):
        return None
    if _hidden_model_ids(ep):
        return None
    from src.endpoint_resolver import chat_capable_models

    return chat_capable_models(cached_ids) or None






def _api_key_fingerprint(api_key: Optional[str]) -> str:
    """Stable, non-secret label for distinguishing same-URL credentials."""
    key = (api_key or "").strip()
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def setup_model_routes(model_discovery):
    router = APIRouter(prefix="/api")

    def _model_owner(request: Request) -> str:
        return effective_user(request) or ""

    def _request_is_admin(request: Request, owner: str = "") -> bool:
        auth_manager = getattr(
            getattr(getattr(request, "app", None), "state", None),
            "auth_manager",
            None,
        )
        is_admin = getattr(auth_manager, "is_admin", None)
        return bool(owner and callable(is_admin) and is_admin(owner))

    def _require_endpoint_owner(request: Request) -> str:
        owner = _model_owner(request)
        auth_manager = getattr(
            getattr(getattr(request, "app", None), "state", None),
            "auth_manager",
            None,
        )
        if not owner or not getattr(request.state, "authenticated", False):
            raise HTTPException(401, "Not authenticated")
        return owner

    def _validate_owner_endpoint_url(
        request: Request,
        owner: str,
        base_url: str,
    ) -> None:
        if not owner or _request_is_admin(request, owner):
            return
        from src.url_security import validate_public_http_url

        try:
            validate_public_http_url(base_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def _owned_endpoint_query(db, request: Request):
        return owner_filter(
            db.query(ModelEndpoint),
            ModelEndpoint,
            _model_owner(request),
            include_shared=False,
        )

    # ---- Model list cache ----
    import time as _time
    # Per-user cache: { owner_key: {"data": ..., "time": ...} }. owner_key is
    # the username (or "" for the unconfigured / single-user case). Without
    # this every user shared the same cached result and the picker showed
    # whichever admin's endpoint list happened to populate it first.
    _models_cache: dict = {}
    _MODELS_CACHE_TTL = 30  # seconds

    def _invalidate_models_cache(owner: str | None = None) -> None:
        """Invalidate all cached catalogues or one owner's catalogue."""
        if owner is None:
            _models_cache.clear()
        else:
            owner_key = (owner or "").strip().lower()
            for key in list(_models_cache):
                if len(key) > 1 and key[1] == owner_key:
                    _models_cache.pop(key, None)
        invalidate_model_catalogue_revision(owner)

    def _schedule_mimo_reprojection(request: Request) -> None:
        """Endpoint registry mutated: recycle mimo workers so the next turn
        respawns them with a freshly projected provider config (the config
        is spawn-time env). CRUD sites only — prefs flips and background
        model refreshes must not churn live workers."""
        import asyncio

        supervisor = getattr(getattr(request, "app", None), "state", None)
        supervisor = getattr(supervisor, "mimo_supervisor", None)
        refresh = getattr(supervisor, "refresh_endpoint_projection", None)
        if refresh is None:
            return
        try:
            asyncio.get_running_loop().create_task(refresh())
        except RuntimeError:
            logger.debug("no running loop; mimo reprojection deferred to next spawn")

    def _direct_share_source(db, owner: str, endpoint_id: str, model_id: str):
        endpoint = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == endpoint_id,
            ModelEndpoint.owner == owner,
            ModelEndpoint.is_enabled == True,  # noqa: E712
        ).first()
        if endpoint is None or (endpoint.model_type or "llm") != "llm":
            return None
        return (
            endpoint
            if model_id in _endpoint_visible_model_ids(endpoint)
            else None
        )

    def _share_provider_metadata(share: ModelShare, endpoint=None) -> tuple[str | None, str]:
        """Return explicit provider metadata for legacy share rows.

        Native rows can identify a family from their selected ``mimo:<family>``
        connection. ``mimo:auto`` intentionally remains unresolved because its
        historical row has no family column; a model ID is ambiguous and must
        never be used as a substitute. Direct rows use the endpoint's detected
        provider, which is connection metadata rather than model naming.
        """
        provider_id = None
        if share.source_kind == "native":
            connection_id = normalize_mimo_connection_id(share.source_id)
            if connection_id and connection_id.startswith("mimo:"):
                provider_id = connection_id.split(":", 1)[1] or None
        else:
            detected = _safe_detect_provider(getattr(endpoint, "base_url", "") or "")
            if detected and detected not in {"custom", "openai-compatible"}:
                provider_id = _DIRECT_PROVIDER_TO_MIMO.get(detected, detected)
        return provider_id, provider_family_display_name(provider_id) or "Unknown provider"

    def _share_display_name(share: ModelShare, endpoint=None) -> str:
        """Return the safe explicit provider label used by legacy projections."""
        return _share_provider_metadata(share, endpoint)[1]

    def _share_users(request: Request, owner: str) -> list[str]:
        manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
        list_users = getattr(manager, "list_users", None)
        if not callable(list_users):
            return []
        return sorted({
            str(item.get("username") or "").strip().lower()
            for item in list_users()
            if isinstance(item, dict)
            and str(item.get("username") or "").strip().lower()
            not in {"", owner}
        })

    def _subscribed_shared_catalog(owner: str) -> list[dict]:
        if not owner:
            return []
        db = SessionLocal()
        try:
            from src.model_shares import (
                source_native_auth,
                source_tools_enabled,
            )
            rows = (
                db.query(ModelShare, ModelShareSubscription)
                .join(
                    ModelShareSubscription,
                    ModelShareSubscription.share_id == ModelShare.id,
                )
                .filter(
                    ModelShare.active == True,  # noqa: E712
                    ModelShare.owner != owner,
                    ModelShareSubscription.subscriber == owner,
                    ModelShareSubscription.enabled == True,  # noqa: E712
                )
                .all()
            )
            items = []
            for share, _subscription in rows:
                access = SharedModelAccess(
                    share_id=share.id,
                    actor_owner=owner,
                    credential_owner=share.owner,
                    source_kind=share.source_kind,
                    source_id=share.source_id,
                    model_id=share.model_id,
                    revision=int(share.revision or 1),
                )
                source_endpoint = None
                if share.source_kind == "endpoint":
                    source_endpoint = _direct_share_source(
                        db,
                        share.owner,
                        share.source_id,
                        share.model_id,
                    )
                    if source_endpoint is None:
                        continue
                elif share.source_kind != "native":
                    continue
                elif source_native_auth(db, access) is None:
                    continue
                tools_enabled = source_tools_enabled(db, access)
                endpoint_id = shared_endpoint_id(share.id)
                provider_family_id, provider_name = _share_provider_metadata(
                    share,
                    source_endpoint,
                )
                catalog = build_model_catalog(
                    endpoint_id=endpoint_id,
                    endpoint_url="mimo://acp",
                    model_ids=[share.model_id],
                    primary_ids=[share.model_id],
                    display_names={
                        share.model_id: _model_display_name(share.model_id),
                    },
                    families={
                        share.model_id: provider_name,
                    },
                    discovered=True,
                    entitled=True,
                    capabilities={
                        "chat": True,
                        "tools": tools_enabled,
                        "vision": None,
                    },
                )
                items.append({
                    "host": "shared",
                    "port": 0,
                    "url": "mimo://acp",
                    "models": [share.model_id],
                    "models_display": [_model_display_name(share.model_id)],
                    "models_extra": [],
                    "models_extra_display": [],
                    "catalog": catalog,
                    "endpoint_id": endpoint_id,
                    "endpoint_name": f"{provider_name} · shared by {share.owner}",
                    "category": "api",
                    "endpoint_kind": "proxy",
                    "model_type": "llm",
                    "virtual": True,
                    "read_only": True,
                    "shared": True,
                    "share_id": share.id,
                    "shared_by": share.owner,
                    "provider_family_id": provider_family_id,
                    "provider_display_name": provider_name,
                })
            return items
        finally:
            db.close()

    # Track model-list refreshes by URL+key. This prevents repeated picker/API
    # opens from starting duplicate /models probes, and gives slow/offline
    # providers a cooldown after failures.
    _refresh_state: Dict[str, Dict[str, Any]] = {}
    import threading as _threading
    _refresh_inflight: set[str] = set()
    _refresh_inflight_lock = _threading.Lock()
    _REFRESH_FAILURE_BASE = 300.0
    _REFRESH_FAILURE_MAX = 3600.0

    def _refresh_key(base: str, api_key: Optional[str], owner: str = "") -> str:
        return f"{owner}\x00{base.rstrip('/')}\x00{api_key or ''}"

    def _ts(value: Any) -> float:
        try:
            return float(value.timestamp()) if value else 0.0
        except Exception:
            return 0.0

    def _failure_delay(fails: int, *, empty_local: bool = False) -> float:
        if fails <= 0:
            return 0.0
        if empty_local:
            return min(5.0 * (2 ** max(0, fails - 1)), 30.0)
        return min(_REFRESH_FAILURE_BASE * (2 ** max(0, fails - 1)), _REFRESH_FAILURE_MAX)

    def _should_refresh_endpoint(ep: Any, now: float, force: bool = False) -> tuple[bool, Dict[str, Any]]:
        base = _normalize_base(getattr(ep, "base_url", "") or "")
        kind = _effective_endpoint_kind(ep, base)
        category = _classify_endpoint(base, kind)
        mode = _endpoint_refresh_mode(ep, kind)
        cached = _cached_model_ids(ep)
        key = _refresh_key(
            base,
            getattr(ep, "api_key", None),
            str(getattr(ep, "owner", None) or ""),
        )
        state = _refresh_state.get(key, {})

        info = {
            "id": getattr(ep, "id", ""),
            "base": base,
            "api_key": getattr(ep, "api_key", None),
            "kind": kind,
            "category": category,
            "mode": mode,
            "key": key,
            "timeout": _endpoint_refresh_timeout(ep, category),
        }
        if not base:
            return False, info
        if state.get("inflight"):
            return False, info
        if mode in ("manual", "disabled") and not force:
            return False, info
        fails = int(state.get("fail_count") or 0)
        if fails and not force:
            last_failure = float(state.get("last_failure") or 0.0)
            empty_local = (
                not cached
                and category == "local"
                and str(getattr(ep, "id", "") or "").startswith("local-")
            )
            if now - last_failure < _failure_delay(fails, empty_local=empty_local):
                return False, info
        if cached and not force:
            interval = _endpoint_refresh_interval(ep, category)
            last_good = float(state.get("last_success") or 0.0) or _ts(getattr(ep, "updated_at", None)) or _ts(getattr(ep, "created_at", None))
            if last_good and now - last_good < interval:
                return False, info
        return True, info

    def _refresh_caches_bg(owner: str = "", force: bool = False):
        """Background thread: safely refresh model caches with per-base single-flight.

        The public /api/models path stays cached-first. This refresh never clears
        a non-empty cached model list on timeout/failure, and proxy/manual
        endpoints are skipped unless explicitly forced."""
        import threading
        owner_key = (owner or "").strip().lower()
        with _refresh_inflight_lock:
            if owner_key in _refresh_inflight:
                return
            _refresh_inflight.add(owner_key)

        def _do():
            try:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                db = SessionLocal()
                changed = False
                try:
                    if _disable_stale_cookbook_local_endpoints(db, owner):
                        changed = True
                    endpoint_query = db.query(ModelEndpoint).filter(
                        ModelEndpoint.is_enabled == True
                    )
                    endpoint_query = owner_filter(
                        endpoint_query, ModelEndpoint, owner, include_shared=False
                    )
                    endpoints = endpoint_query.all()
                    now = _time.time()
                    groups: Dict[str, Dict[str, Any]] = {}
                    for ep in endpoints:
                        ok, info = _should_refresh_endpoint(ep, now, force=force)
                        if not ok:
                            continue
                        groups.setdefault(info["key"], {
                            "base": info["base"],
                            "api_key": info["api_key"],
                            "timeout": info["timeout"],
                            "endpoint_ids": [],
                        })["endpoint_ids"].append(info["id"])

                    for key in groups:
                        st = _refresh_state.setdefault(key, {})
                        st["inflight"] = True
                        st["last_attempt"] = now

                    def _probe_one(key: str, data: Dict[str, Any]):
                        try:
                            ids, diagnostic = _probe_endpoint_result(
                                data["base"], data.get("api_key"),
                                timeout=data.get("timeout") or 2,
                            )
                            return key, data["endpoint_ids"], ids, diagnostic, None
                        except Exception as e:
                            return key, data["endpoint_ids"], None, {"status": "unavailable"}, e

                    if groups:
                        with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
                            futures = [pool.submit(_probe_one, key, data) for key, data in groups.items()]
                            for fut in as_completed(futures):
                                key, endpoint_ids, ids, diagnostic, err = fut.result()
                                st = _refresh_state.setdefault(key, {})
                                st["catalog_probe"] = diagnostic
                                for ep_id in endpoint_ids:
                                    ep_obj = db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).first()
                                    if ep_obj:
                                        _record_catalog_probe(ep_obj, diagnostic)
                                if ids:
                                    for ep_id in endpoint_ids:
                                        ep_obj = db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).first()
                                        if ep_obj:
                                            ep_obj.cached_models = json.dumps(ids)
                                            changed = True
                                    st["last_success"] = _time.time()
                                    st["fail_count"] = 0
                                    st.pop("last_failure", None)
                                else:
                                    st["last_failure"] = _time.time()
                                    st["fail_count"] = int(st.get("fail_count") or 0) + 1
                                st["inflight"] = False
                        db.commit()
                finally:
                    db.close()
                if changed:
                    _invalidate_models_cache(owner)
            except Exception as e:
                logger.warning('Background endpoint refresh failed: %s', e)
            finally:
                prefix = f"{owner_key}\x00"
                for key, state in _refresh_state.items():
                    if key.startswith(prefix):
                        state["inflight"] = False
                with _refresh_inflight_lock:
                    _refresh_inflight.discard(owner_key)
        threading.Thread(target=_do, daemon=True).start()

    def _fetch_models(owner: str = ""):
        """Return model list from cached data (instant). Background refresh keeps caches fresh.

        Authenticated callers see exact-owner rows only. NULL-owner records
        are legacy/unclaimed and startup assigns them to the first admin.
        """
        items = []

        db = SessionLocal()
        try:
            if _disable_stale_cookbook_local_endpoints(db, owner):
                _invalidate_models_cache(owner)
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
            q = owner_filter(q, ModelEndpoint, owner, include_shared=False)
            endpoints = q.all()
        finally:
            db.close()

        for ep in endpoints:
            base = _normalize_base(ep.base_url)
            provider = _safe_detect_provider(base)
            ep_model_type = getattr(ep, "model_type", None) or "llm"
            # Build correct URL based on provider
            chat_url = build_chat_url(base)
            kind = _effective_endpoint_kind(ep, base)
            category = _classify_endpoint(base, kind)
            model_ids, pinned = _picker_models_for_endpoint(ep, base, kind)

            if model_ids:
                curated_key = _match_provider_curated(base, None)
                curated, extra = _curate_models(model_ids, curated_key)
                # Pinned models are admin-selected — they always belong in the
                # primary curated list, not buried in extras.
                for m in pinned:
                    if m not in curated:
                        curated.append(m)
                extra = [m for m in extra if m not in pinned]
                items.append({
                    "host": "custom",
                    "port": 0,
                    "url": chat_url,
                    "models": curated,
                    "models_display": [_model_display_name(mid) for mid in curated],
                    "models_extra": extra,
                    "models_extra_display": [_model_display_name(mid) for mid in extra],
                    "endpoint_id": ep.id,
                    "endpoint_name": ep.name,
                    "category": category,
                    "endpoint_kind": kind,
                    "model_type": ep_model_type,
                    "catalog": build_model_catalog(
                        endpoint_id=ep.id,
                        endpoint_url=chat_url,
                        model_ids=model_ids,
                        primary_ids=curated,
                        extra_ids=extra,
                        discovered=True,
                        entitled=_catalog_entitlement(base, model_ids),
                        stale=False,
                        capabilities={
                            "chat": ep_model_type == "llm",
                            "tools": getattr(ep, "supports_tools", None),
                            "vision": None,
                        },
                    ),
                })
            else:
                # Endpoint unreachable but still show it greyed out
                items.append({
                    "host": "custom",
                    "port": 0,
                    "url": chat_url,
                    "models": [],
                    "models_display": [],
                    "models_extra": [],
                    "models_extra_display": [],
                    "endpoint_id": ep.id,
                    "endpoint_name": ep.name,
                    "category": category,
                    "endpoint_kind": kind,
                    "model_type": ep_model_type,
                    "catalog": [],
                    "offline": True,
                })

        return {"hosts": [], "items": items}

    @router.get("/models")
    def api_models(request: Request, refresh: bool = False, background: bool = False):
        """Return a read-only projection of normalized managed chat routes.

        ``refresh`` and ``background`` remain accepted for wire compatibility,
        but this endpoint never probes providers or wakes a worker. Catalog
        discovery and credential semantics belong to the managed engine.
        """
        # Require auth; "" is unconfigured single-user mode and resolves to
        # the normalized local-installation principal.
        try:
            if getattr(request.state, "api_token", False):
                scopes = set(getattr(request.state, "api_token_scopes", []) or [])
                if "chat" not in scopes:
                    raise HTTPException(403, "API token is not scoped for chat")
                if not getattr(request.state, "api_token_owner", None):
                    raise HTTPException(403, "API token has no owner")
            owner = (effective_user(request) or "").strip().lower()

            # Reject anonymous in configured deployments — no leaking the model
            # list to unauthenticated callers.
            auth_mgr = getattr(request.app.state, "auth_manager", None)
            if not owner and auth_mgr is not None and getattr(auth_mgr, "is_configured", False):
                raise HTTPException(401, "Not authenticated")
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Auth gate error in GET /api/models, failing closed: %s", e)
            raise HTTPException(status_code=500, detail="Internal error")
        try:
            own_routes, shared_routes = list_chat_routes(owner)
        except Exception as exc:
            logger.error("Normalized provider catalogue failed closed: %s", exc)
            raise HTTPException(503, "Provider catalogue is unavailable") from exc

        def _project_group(endpoint_id: str, routes: list) -> dict:
            first = routes[0]
            model_ids = [route.provider_model_id for route in routes]
            displays = [route.display_name for route in routes]
            catalog = []
            for route in routes:
                capabilities = dict(route.capabilities or {})
                capabilities["chat"] = True
                record = build_model_catalog(
                    endpoint_id=endpoint_id,
                    endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
                    model_ids=[route.provider_model_id],
                    primary_ids=[route.provider_model_id],
                    display_names={route.provider_model_id: route.display_name},
                    families={
                        route.provider_model_id: (
                            None if route.shared else route.family_id
                        )
                    },
                    discovered=True,
                    entitled=True,
                    capabilities=capabilities,
                )[0]
                record.update({
                    "provider_model_id": route.provider_model_id,
                    "operations": list(route.operations),
                })
                if not route.shared:
                    record["provider_model_route_id"] = route.model_route_id
                else:
                    # Private filtering aid removed before the response leaves
                    # this function. Shared source route IDs are not public UI
                    # selectors; the accepted grant is.
                    record["_provider_model_route_id"] = route.model_route_id
                catalog.append(record)

            endpoint_name = first.share_label or first.connection_label
            if first.disclosed_owner:
                endpoint_name = f"{endpoint_name} · shared by {first.disclosed_owner}"
            item = {
                "host": "managed",
                "port": 0,
                "url": MANAGED_ENGINE_PUBLIC_URL,
                "models": model_ids,
                "models_display": displays,
                "models_extra": [],
                "models_extra_display": [],
                "catalog": catalog,
                "endpoint_id": endpoint_id,
                "endpoint_name": endpoint_name,
                "category": (
                    "api"
                    if first.shared
                    else ("local" if first.connection_kind == "local" else "api")
                ),
                "endpoint_kind": "shared" if first.shared else first.connection_kind,
                "model_type": "llm",
                "virtual": True,
                "read_only": True,
                "actions": ["configure_providers"],
                "settings_tab": "providers",
            }
            if first.shared:
                item.update({
                    "shared": True,
                    "share_id": first.provider_grant_id,
                })
                if first.disclosed_owner:
                    item["shared_by"] = first.disclosed_owner
            else:
                item["connection_id"] = first.connection_id
                item["billing_lane"] = first.billing_lane
                item["family_id"] = first.family_id
            return item

        result = {"hosts": [], "items": []}
        for endpoint_id, routes in sorted(group_chat_routes(own_routes).items()):
            result["items"].append(_project_group(endpoint_id, routes))
        for endpoint_id, routes in sorted(group_chat_routes(shared_routes).items()):
            result["items"].append(_project_group(endpoint_id, routes))
        result = _filter_catalog_for_allowed_models(
            result,
            _allowed_model_ids(request, owner),
        )
        for item in result["items"]:
            for record in item.get("catalog") or ():
                record.pop("_provider_model_route_id", None)
        return result

    # Brief cache for local-probe results so picker-open doesn't hammer
    # endpoint health checks every time. 8s TTL — long enough to amortize cost,
    # short enough that a freshly-killed local server shows as offline
    # within ~8s of the user noticing.
    _LOCAL_PROBE_TTL = 8.0
    _local_probe_cache: Dict[str, Dict[str, Any]] = {}
    _local_probe_inflight: Dict[str, Any] = {}

    @router.get("/model-endpoints/probe-local")
    async def probe_local_endpoints(request: Request):
        """Fast parallel reachability check for LOCAL endpoints only.
        Cloud endpoints (api.openai.com, api.anthropic.com, etc.) are
        assumed up. Local endpoints get a 1.5s cheap reachability probe so the UI
        can dim stale entries pointing at dead vLLM servers. Returns
        {ep_id: {alive, latency_ms, error}}."""
        owner = _require_endpoint_owner(request)
        now = _time.time()
        cache_entry = _local_probe_cache.get(owner)
        if (
            cache_entry is not None
            and cache_entry.get("revision") == model_catalogue_revision(owner)
            and (now - cache_entry["time"]) < _LOCAL_PROBE_TTL
        ):
            return cache_entry["data"]

        import asyncio as _asyncio
        task = _local_probe_inflight.get(owner)
        if task is not None and not task.done():
            return await task

        async def _compute_local_probe() -> Dict[str, Any]:
            db = SessionLocal()
            try:
                if _disable_stale_cookbook_local_endpoints(db, owner):
                    _invalidate_models_cache(owner)
                query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
                query = owner_filter(query, ModelEndpoint, owner, include_shared=False)
                endpoints = query.all()
                local_eps = []
                for ep in endpoints:
                    base = _normalize_base(ep.base_url)
                    kind = _effective_endpoint_kind(ep, base)
                    if _classify_endpoint(base, kind) == "local":
                        local_eps.append((ep.id, base, ep.api_key))
            finally:
                db.close()

            grouped: Dict[str, Dict[str, Any]] = {}
            for ep_id, base, api_key in local_eps:
                key = _refresh_key(base, api_key, owner)
                grouped.setdefault(key, {"base": base, "api_key": api_key, "endpoint_ids": []})["endpoint_ids"].append(ep_id)

            async def _probe_one(data: Dict[str, Any]) -> Dict[str, Any]:
                t0 = _time.time()
                try:
                    # Bumped 1.5s → 3.5s. The previous 1.5s budget was clipping
                    # local vLLM endpoints on Tailscale links where the model
                    # server is still loading (Qwen3.5-122B takes 2–3 min to
                    # warm); /v1/models can take 500–2500 ms on a busy box,
                    # which pushed _ping_endpoint's full path-discovery sweep
                    # past the cap and marked the row offline despite the
                    # user actively chatting with it.
                    ping = await _asyncio.to_thread(_ping_endpoint, data["base"], data.get("api_key"), 3.5)
                    lat = round((_time.time() - t0) * 1000)
                    return {
                        "alive": bool(ping.get("reachable")),
                        "latency_ms": lat,
                        "status_code": ping.get("status_code"),
                        "error": ping.get("error"),
                    }
                except Exception as e:
                    return {"alive": False, "latency_ms": None, "status_code": None, "error": str(e)[:120]}

            results_list = await _asyncio.gather(
                *[_probe_one(data) for data in grouped.values()],
                return_exceptions=False,
            )
            results: Dict[str, Any] = {}
            for data, r in zip(grouped.values(), results_list):
                for eid in data["endpoint_ids"]:
                    results[eid] = r

            _local_probe_cache[owner] = {
                "data": results,
                "time": _time.time(),
                "revision": model_catalogue_revision(owner),
            }
            return results

        task = _asyncio.create_task(_compute_local_probe())
        _local_probe_inflight[owner] = task
        try:
            return await task
        finally:
            if _local_probe_inflight.get(owner) is task:
                _local_probe_inflight.pop(owner, None)

    @router.get("/ping")
    def ping_endpoints(request: Request):
        """Probe all enabled endpoints and return status + latency."""
        require_admin(request)
        db = SessionLocal()
        try:
            endpoints = _owned_endpoint_query(db, request).filter(
                ModelEndpoint.is_enabled == True
            ).all()
        finally:
            db.close()

        results = []
        for ep in endpoints:
            base = _normalize_base(ep.base_url)
            provider = _safe_detect_provider(base)
            kind = _effective_endpoint_kind(ep, base)
            cached_count = len(_cached_model_ids(ep))
            entry = {
                "id": ep.id,
                "name": ep.name,
                "base_url": base,
                "provider": provider,
                "category": _classify_endpoint(base, kind),
                "endpoint_kind": kind,
            }
            try:
                t0 = _time.time()
                ping = _ping_endpoint(base, ep.api_key, timeout=1.5)
                entry["latency_ms"] = round((_time.time() - t0) * 1000)
                entry["status"] = "loading" if ping.get("loading") else ("online" if ping.get("reachable") or cached_count else "offline")
                entry["error"] = ping.get("error")
                entry["model_count"] = cached_count or (len(ANTHROPIC_MODELS) if provider == "anthropic" else 0)
            except Exception as e:
                entry["latency_ms"] = None
                entry["status"] = "online" if cached_count else "offline"
                entry["error"] = str(e)
                entry["model_count"] = cached_count
            results.append(entry)

        return {"endpoints": results}

    @router.post("/probe-selected")
    def probe_selected(request: Request, request_body: dict = Body(...)):
        """Probe specific models for compare pre-check. Body: {models: [{endpoint_id, model}]}."""
        require_admin(request)
        models_to_probe = request_body.get("models", [])
        if not models_to_probe:
            return {"results": []}

        db = SessionLocal()
        try:
            endpoints_cache = {}
            results = []
            for item in models_to_probe:
                ep_id = item.get("endpoint_id", "")
                model_id = item.get("model", "")
                if not model_id:
                    results.append({"model": model_id, "status": "fail", "error": "No model specified"})
                    continue

                # Cache endpoint lookups
                if ep_id and ep_id not in endpoints_cache:
                    ep = _owned_endpoint_query(db, request).filter(
                        ModelEndpoint.id == ep_id
                    ).first()
                    if ep:
                        endpoints_cache[ep_id] = {"base_url": ep.base_url, "api_key": ep.api_key}
                ep_data = endpoints_cache.get(ep_id)
                if not ep_data:
                    # Try to find by base_url from the model's endpoint field
                    endpoint_url = item.get("endpoint", "")
                    if endpoint_url:
                        ep_data = {"base_url": endpoint_url, "api_key": item.get("api_key", "")}
                    else:
                        results.append({"model": model_id, "status": "fail", "error": "Endpoint not found"})
                        continue

                base = _normalize_base(ep_data["base_url"])
                _with_tools = item.get("with_tools", False)
                result = _probe_single_model(base, ep_data.get("api_key"), model_id, timeout=8, with_tools=_with_tools, owner=_model_owner(request), endpoint_id=ep_id)
                result["model"] = model_id
                result["endpoint_id"] = ep_id
                results.append(result)

            return {"results": results}
        finally:
            db.close()

    @router.get("/probe")
    def probe_models(request: Request, endpoint_id: Optional[str] = Query(None)):
        """Probe individual models with a tiny completion request. Streams SSE results."""
        require_admin(request)
        db = SessionLocal()
        try:
            q = _owned_endpoint_query(db, request).filter(ModelEndpoint.is_enabled == True)
            if endpoint_id:
                q = q.filter(ModelEndpoint.id == endpoint_id)
            endpoints = q.all()
            # Detach from session
            ep_data = []
            for ep in endpoints:
                ep_data.append({
                    "id": ep.id,
                    "name": ep.name,
                    "base_url": ep.base_url,
                    "api_key": ep.api_key,
                })
        finally:
            db.close()

        if not ep_data:
            def _empty():
                yield f"data: {json.dumps({'type': 'probe_done', 'total': 0, 'ok': 0})}\n\n"
            return StreamingResponse(_empty(), media_type="text/event-stream")

        def _stream():
            total = 0
            ok_count = 0
            for ep in ep_data:
                base = _normalize_base(ep["base_url"])
                all_models, diagnostic = _probe_endpoint_result(base, ep.get("api_key"))
                db_probe = SessionLocal()
                try:
                    ep_obj = _owned_endpoint_query(db_probe, request).filter(
                        ModelEndpoint.id == ep["id"]
                    ).first()
                    if ep_obj:
                        _record_catalog_probe(ep_obj, diagnostic)
                        db_probe.commit()
                finally:
                    db_probe.close()
                # Update cached_models in DB
                if all_models:
                    db2 = SessionLocal()
                    try:
                        ep_obj = _owned_endpoint_query(db2, request).filter(
                            ModelEndpoint.id == ep["id"]
                        ).first()
                        if ep_obj:
                            ep_obj.cached_models = json.dumps(all_models)
                            db2.commit()
                    finally:
                        db2.close()
                if not all_models:
                    yield f"data: {json.dumps({'type': 'probe_start', 'endpoint': ep['name'], 'model_count': 0, 'error': 'No models found or endpoint offline'})}\n\n"
                    continue

                models = [m for m in all_models if _is_chat_model(m)]
                skipped = len(all_models) - len(models)
                yield f"data: {json.dumps({'type': 'probe_start', 'endpoint': ep['name'], 'model_count': len(models), 'skipped': skipped})}\n\n"

                for model_id in models:
                    total += 1
                    result = _probe_single_model(base, ep.get("api_key"), model_id, timeout=8, owner=_model_owner(request), endpoint_id=ep["id"])
                    result["type"] = "probe_result"
                    result["endpoint"] = ep["name"]
                    result["model"] = model_id
                    if result["status"] == "ok":
                        ok_count += 1
                    yield f"data: {json.dumps(result)}\n\n"

            yield f"data: {json.dumps({'type': 'probe_done', 'total': total, 'ok': ok_count})}\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    # /api/providers runs a full host port-scan (discover_models) which can take
    # seconds when a configured LLM host is unreachable. It's fetched on every
    # page load, so cache it briefly like _models_cache to keep page load snappy.
    _providers_cache = {"data": None, "time": 0}
    _PROVIDERS_CACHE_TTL = 30  # seconds

    @router.get("/providers")
    def providers(request: Request, refresh: bool = False):
        """Get all available providers (cached for 30s)."""
        require_admin(request)
        now = _time.time()
        if not refresh and _providers_cache["data"] is not None and (now - _providers_cache["time"]) < _PROVIDERS_CACHE_TTL:
            return _providers_cache["data"]
        result = model_discovery.get_providers()
        _providers_cache["data"] = result
        _providers_cache["time"] = now
        return result

    @router.get("/discover")
    def discover_local(request: Request):
        """Scan local network for model servers on common ports."""
        require_admin(request)
        return model_discovery.discover_models()

    @router.get("/model-shares")
    def list_model_shares(request: Request):
        """List exact named-user grants owned by or addressed to the caller."""
        owner = _require_endpoint_owner(request)
        if not owner:
            raise HTTPException(401, "Model sharing requires an account")
        allowed = _allowed_model_ids(request, owner)
        db = SessionLocal()
        try:
            owned_rows = db.query(ModelShare).filter(
                ModelShare.owner == owner,
                ModelShare.active == True,  # noqa: E712
            ).all()
            owned_ids = [share.id for share in owned_rows]
            recipients: dict[str, list[dict]] = {
                share_id: [] for share_id in owned_ids
            }
            if owned_ids:
                for grant in db.query(ModelShareSubscription).filter(
                    ModelShareSubscription.share_id.in_(owned_ids)
                ).all():
                    recipients.setdefault(grant.share_id, []).append({
                        "username": grant.subscriber,
                        "enabled": bool(grant.enabled),
                    })
            received = []
            addressed = (
                db.query(ModelShare, ModelShareSubscription)
                .join(
                    ModelShareSubscription,
                    ModelShareSubscription.share_id == ModelShare.id,
                )
                .filter(
                    ModelShare.owner != owner,
                    ModelShare.active == True,  # noqa: E712
                    ModelShareSubscription.subscriber == owner,
                )
                .all()
            )
            from src.model_shares import (
                source_native_auth,
                source_tools_enabled,
            )

            for share, grant in addressed:
                if allowed is not None and share.model_id not in allowed:
                    continue
                access = SharedModelAccess(
                    share_id=share.id,
                    actor_owner=owner,
                    credential_owner=share.owner,
                    source_kind=share.source_kind,
                    source_id=share.source_id,
                    model_id=share.model_id,
                    revision=int(share.revision or 1),
                )
                source_endpoint = None
                if share.source_kind == "endpoint":
                    source_endpoint = _direct_share_source(
                        db,
                        share.owner,
                        share.source_id,
                        share.model_id,
                    )
                    if source_endpoint is None:
                        continue
                elif share.source_kind != "native":
                    continue
                elif source_native_auth(db, access) is None:
                    continue
                provider_family_id, provider_name = _share_provider_metadata(
                    share,
                    source_endpoint,
                )
                received.append({
                    "share_id": share.id,
                    "endpoint_id": shared_endpoint_id(share.id),
                    "model_id": share.model_id,
                    "model_name": _model_display_name(share.model_id),
                    "provider": provider_name,
                    "provider_family_id": provider_family_id,
                    "provider_display_name": provider_name,
                    "shared_by": share.owner,
                    "enabled": bool(grant.enabled),
                    "tools": source_tools_enabled(db, access),
                })
            return {
                "share_users": [
                    {"username": username}
                    for username in _share_users(request, owner)
                ],
                "owned": [
                    {
                        "share_id": share.id,
                        "endpoint_id": share.source_id,
                        "model_id": share.model_id,
                        "source_kind": share.source_kind,
                        "recipients": sorted(
                            recipients.get(share.id, []),
                            key=lambda item: item["username"].casefold(),
                        ),
                    }
                    for share in owned_rows
                ],
                "received": sorted(
                    received,
                    key=lambda item: (
                        item["provider"].casefold(),
                        item["model_name"].casefold(),
                        item["shared_by"].casefold(),
                    ),
                ),
            }
        finally:
            db.close()

    @router.put("/model-shares")
    async def update_model_share(
        payload: ModelShareUpdate,
        request: Request,
    ):
        """Publish or revoke one exact owner-controlled model route."""
        owner = _require_endpoint_owner(request)
        if not owner:
            raise HTTPException(401, "Model sharing requires an account")
        endpoint_id = payload.endpoint_id.strip()
        model_id = public_native_model_id(payload.model_id)
        if not endpoint_id or not model_id:
            raise HTTPException(400, "endpoint_id and model_id are required")

        connection_id = normalize_mimo_connection_id(endpoint_id)
        source_kind = "native" if connection_id else "endpoint"
        source_id = connection_id or endpoint_id
        recipients = {
            str(recipient or "").strip().lower()
            for recipient in payload.recipients
            if str(recipient or "").strip()
        }
        known_recipients = set(_share_users(request, owner))
        unknown = sorted(recipients - known_recipients)
        if owner in recipients:
            unknown.append(owner)
        if unknown:
            raise HTTPException(
                400,
                f"Unknown share recipient: {unknown[0]}",
            )
        db = SessionLocal()
        try:
            if source_kind == "endpoint":
                if _direct_share_source(db, owner, source_id, model_id) is None:
                    raise HTTPException(404, "Owned model route not found")
            else:
                if model_id == "xiaomi/mimo-auto":
                    raise HTTPException(
                        400,
                        "MiMo Auto is already included for every Open Clank user",
                    )
                supervisor = getattr(request.app.state, "mimo_supervisor", None)
                if supervisor is None:
                    raise HTTPException(503, "Open Clank agent runtime is unavailable")
                worker = supervisor
                starter = getattr(supervisor, "for_owner", None)
                if callable(starter):
                    try:
                        worker = await starter(owner)
                    except RuntimeError as exc:
                        raise HTTPException(503, str(exc)) from exc
                if model_id not in mimo_connection_model_ids(
                    worker,
                    owner,
                    source_id,
                ):
                    raise HTTPException(404, "Owned model route not found")

            existing = db.query(ModelShare).filter(
                ModelShare.owner == owner,
                ModelShare.source_kind == source_kind,
                ModelShare.source_id == source_id,
                ModelShare.model_id == model_id,
            ).first()
            if not payload.shared:
                if existing is not None:
                    revocations = [
                        (grant.subscriber, existing.id, existing.model_id)
                        for grant in db.query(ModelShareSubscription).filter(
                            ModelShareSubscription.share_id == existing.id,
                            ModelShareSubscription.enabled == True,  # noqa: E712
                        ).all()
                    ]
                    db.query(ModelShareSubscription).filter(
                        ModelShareSubscription.share_id == existing.id
                    ).delete(synchronize_session=False)
                    db.delete(existing)
                    db.commit()
                    _invalidate_models_cache()
                    _schedule_mimo_reprojection(request)
                    await revoke_shared_model_workers(request, revocations)
                    notify_shared_model_removals(revocations, shared_by=owner)
                return {"shared": False}
            created = existing is None
            if created:
                existing = ModelShare(
                    id=uuid.uuid4().hex,
                    owner=owner,
                    source_kind=source_kind,
                    source_id=source_id,
                    model_id=model_id,
                    active=True,
                    revision=1,
                )
                db.add(existing)
                db.flush()
            current = {
                grant.subscriber: grant
                for grant in db.query(ModelShareSubscription).filter(
                    ModelShareSubscription.share_id == existing.id
                ).all()
            }
            changed_recipients = set(current) != recipients
            revocations = [
                (username, existing.id, existing.model_id)
                for username, grant in current.items()
                if username not in recipients and grant.enabled
            ]
            for username, grant in current.items():
                if username not in recipients:
                    db.delete(grant)
            for username in recipients - set(current):
                db.add(ModelShareSubscription(
                    share_id=existing.id,
                    subscriber=username,
                    enabled=False,
                ))
            if changed_recipients:
                existing.revision = int(existing.revision or 1) + 1
            if created or changed_recipients:
                db.commit()
                _invalidate_models_cache()
                _schedule_mimo_reprojection(request)
                await revoke_shared_model_workers(request, revocations)
                notify_shared_model_removals(revocations, shared_by=owner)
            return {
                "shared": True,
                "share_id": existing.id,
                "endpoint_id": source_id,
                "model_id": model_id,
                "recipients": sorted(recipients),
            }
        finally:
            db.close()

    @router.put("/model-shares/{share_id}/subscription")
    async def update_model_share_subscription(
        share_id: str,
        payload: ModelShareSubscriptionUpdate,
        request: Request,
    ):
        """Opt the caller into or out of one active published route."""
        owner = _require_endpoint_owner(request)
        if not owner:
            raise HTTPException(401, "Model sharing requires an account")
        db = SessionLocal()
        try:
            share = db.query(ModelShare).filter(
                ModelShare.id == share_id,
                ModelShare.active == True,  # noqa: E712
            ).first()
            if share is None or share.owner == owner:
                raise HTTPException(404, "Shared model not found")
            allowed = _allowed_model_ids(request, owner)
            if allowed is not None and share.model_id not in allowed:
                raise HTTPException(403, "This account is not allowed to use that model")
            subscription = db.query(ModelShareSubscription).filter(
                ModelShareSubscription.share_id == share.id,
                ModelShareSubscription.subscriber == owner,
            ).first()
            if subscription is None:
                raise HTTPException(404, "This model was not shared with your account")
            if not payload.enabled:
                subscription.enabled = False
                db.commit()
                _invalidate_models_cache(owner)
                _schedule_mimo_reprojection(request)
                await revoke_shared_model_workers(
                    request,
                    [(owner, share.id, share.model_id)],
                )
                return {"enabled": False}
            subscription.enabled = True
            db.commit()
            _invalidate_models_cache(owner)
            _schedule_mimo_reprojection(request)
            return {
                "enabled": True,
                "endpoint_id": shared_endpoint_id(share.id),
                "model_id": share.model_id,
            }
        finally:
            db.close()

    # ---- Owner-scoped model endpoints CRUD ----

    @router.get("/model-endpoints")
    def list_model_endpoints(request: Request) -> List[Dict[str, Any]]:
        owner = _require_endpoint_owner(request)
        db = SessionLocal()
        try:
            if _disable_stale_cookbook_local_endpoints(db, owner):
                _invalidate_models_cache(owner)
            rows = _owned_endpoint_query(db, request).order_by(ModelEndpoint.created_at).all()
            shared_routes = {
                (share.source_kind, share.source_id, share.model_id)
                for share in db.query(ModelShare).filter(
                    ModelShare.owner == owner,
                    ModelShare.active == True,  # noqa: E712
                ).all()
            }
            results = []
            from src.model_capabilities import endpoint_capability_states
            for r in rows:
                all_models = _cached_model_ids(r)
                hidden = _hidden_model_ids(r)
                pinned = _normalize_model_ids(getattr(r, "pinned_models", None))
                visible = _visible_models(all_models, r.hidden_models, pinned)
                model_capabilities = endpoint_capability_states(db, r)
                _cap_by_model = {item["model_id"]: item for item in model_capabilities}
                _visible_caps = [_cap_by_model[m] for m in visible if m in _cap_by_model]
                tools_summary = (
                    True if _visible_caps and all(item["eligible"] for item in _visible_caps)
                    else False if any(item["status"] == "blocked" for item in _visible_caps)
                    else None
                )
                # Keep the list route cache-only. It feeds Settings →
                # Added Models and must render immediately; explicit
                # Refresh/Probe endpoints do the network work.
                ping = None
                # When cached_models is empty, do a quick reachability probe.
                # Bumped 1.0s → 3.5s because the user reported endpoints they
                # were ACTIVELY chatting with showed "offline" — the previous
                # 1s timeout was clipping live cloud endpoints (DeepSeek can
                # take 1.5–2.5s on /v1/models when their region is under load,
                # vLLM on a remote GPU box behind SSH can also push past 1s).
                # 3.5s still keeps the picker render snappy in the common
                # "everything's already cached" path because this branch only
                # runs for endpoints with an empty cached_models.
                if not all_models and not pinned and r.is_enabled:
                    base_for_ping = _normalize_base(r.base_url)
                    kind_for_ping = _effective_endpoint_kind(r, base_for_ping)
                    ping_timeout = 10.0 if _classify_endpoint(base_for_ping, kind_for_ping) == "local" else 3.5
                    ping = _ping_endpoint(r.base_url, r.api_key, timeout=ping_timeout)
                    if ping.get("reachable"):
                        status = "loading" if ping.get("loading") else "empty"
                        if ping.get("loading"):
                            base = _normalize_base(r.base_url)
                            kind = _effective_endpoint_kind(r, base)
                            catalog_primary, catalog_extra = _curate_models(
                                visible, _match_provider_curated(base, _safe_detect_provider(base))
                            )
                            results.append({
                                "id": r.id,
                                "name": r.name,
                                "base_url": r.base_url,
                                "has_key": bool(r.api_key),
                                "api_key_fingerprint": _api_key_fingerprint(r.api_key),
                                "is_enabled": r.is_enabled,
                                "models": visible,
                                "shared_models": [
                                    model_id
                                    for model_id in visible
                                    if ("endpoint", r.id, model_id) in shared_routes
                                ],
                                "pinned_models": pinned,
                                "hidden_count": len(hidden),
                                "online": True,
                                "status": status,
                                "ping_error": (ping or {}).get("error") if ping else None,
                                "model_type": getattr(r, "model_type", None) or "llm",
                                "catalog": build_model_catalog(
                                    endpoint_id=r.id,
                                    endpoint_url=build_chat_url(base),
                                    model_ids=visible,
                                    primary_ids=catalog_primary,
                                    extra_ids=catalog_extra,
                                    discovered=bool(all_models),
                                    entitled=_catalog_entitlement(base, visible),
                                    stale=status != "online",
                                    capabilities={
                                        "chat": (getattr(r, "model_type", None) or "llm") == "llm",
                                        "tools": tools_summary,
                                        "vision": None,
                                    },
                                ),
                                "supports_tools": tools_summary,
                                "model_capabilities": model_capabilities,
                                "catalog_probe": _catalog_probe_payload(r),
                                "endpoint_kind": kind,
                                "category": _classify_endpoint(base, kind),
                                "model_refresh_mode": _endpoint_refresh_mode(r, kind),
                                "model_refresh_interval": getattr(r, "model_refresh_interval", None),
                                "model_refresh_timeout": getattr(r, "model_refresh_timeout", None),
                            })
                            continue
                        # Best-effort: if the probe came back reachable, try
                        # to populate cached_models in the background so the
                        # NEXT picker load shows "online" instead of "empty".
                        # Failure here is silent — we already returned the
                        # "empty" status, and the existing background refresh
                        # path will eventually fill it in too.
                        try:
                            probed, diagnostic = _probe_endpoint_result(
                                r.base_url,
                                r.api_key,
                                timeout=max(5, int(ping_timeout)),
                            )
                            _record_catalog_probe(r, diagnostic)
                            db.commit()
                            if probed:
                                r.cached_models = json.dumps(probed)
                                db.commit()
                                all_models = probed
                                visible = _visible_models(all_models, r.hidden_models, pinned)
                                status = "online"
                        except Exception as _refill_err:
                            logger.debug(f"opportunistic cached_models refill failed for {r.id}: {_refill_err!r}")
                base = _normalize_base(r.base_url)
                kind = _effective_endpoint_kind(r, base)
                visible, pinned = _picker_models_for_endpoint(r, base, kind)
                model_inventory_count = len(_merge_model_ids(all_models, pinned))
                picker_requires_pinning = _picker_requires_pinning(base, kind)
                status = "online" if (all_models or visible or pinned) else ("empty" if r.is_enabled else "offline")
                model_capabilities = endpoint_capability_states(db, r)
                _cap_by_model = {
                    item["model_id"]: item for item in model_capabilities
                }
                _visible_caps = [
                    _cap_by_model[m] for m in visible if m in _cap_by_model
                ]
                tools_summary = (
                    True
                    if _visible_caps and all(item["eligible"] for item in _visible_caps)
                    else False
                    if any(item["status"] == "blocked" for item in _visible_caps)
                    else None
                )
                catalog_primary, catalog_extra = _curate_models(
                    visible, _match_provider_curated(base, _safe_detect_provider(base))
                )
                results.append({
                    "id": r.id,
                    "name": r.name,
                    "base_url": r.base_url,
                    "has_key": bool(r.api_key),
                    "api_key_fingerprint": _api_key_fingerprint(r.api_key),
                    "is_enabled": r.is_enabled,
                    "models": visible,
                    "shared_models": [
                        model_id
                        for model_id in visible
                        if ("endpoint", r.id, model_id) in shared_routes
                    ],
                    "model_count": model_inventory_count,
                    "picker_requires_pinning": picker_requires_pinning,
                    "pinned_models": pinned,
                    "hidden_count": len(hidden),
                    "online": status != "offline",
                    "status": status,
                    "ping_error": (ping or {}).get("error") if ping else None,
                    "model_type": getattr(r, "model_type", None) or "llm",
                    "catalog": build_model_catalog(
                        endpoint_id=r.id,
                        endpoint_url=build_chat_url(base),
                        model_ids=visible,
                        primary_ids=catalog_primary,
                        extra_ids=catalog_extra,
                        discovered=bool(all_models),
                        entitled=_catalog_entitlement(base, visible),
                        stale=status != "online",
                        capabilities={
                            "chat": (getattr(r, "model_type", None) or "llm") == "llm",
                            "tools": tools_summary,
                            "vision": None,
                        },
                    ),
                    "supports_tools": tools_summary,
                    "model_capabilities": model_capabilities,
                    "catalog_probe": _catalog_probe_payload(r),
                    "endpoint_kind": kind,
                    "category": _classify_endpoint(base, kind),
                    "model_refresh_mode": _endpoint_refresh_mode(r, kind),
                    "model_refresh_interval": getattr(r, "model_refresh_interval", None),
                    "model_refresh_timeout": getattr(r, "model_refresh_timeout", None),
                })
            # Keep the legacy automatic route available to defaults and other
            # selectors. Added Models hides selector-only rows and renders the
            # actual provider accounts instead.
            _sup = getattr(getattr(getattr(request, "app", None), "state", None), "mimo_supervisor", None)
            ensure_owner_mimo_worker(_sup, owner)
            _mimo_models, _base, _variants, _hidden_count = _mimo_catalog(
                _sup, owner
            )
            # The aggregate drops owner-hidden models with the rest; report
            # how many were hidden so selector totals stay truthful.
            _owner_hidden_ids = _owner_hidden_mimo_model_ids(owner)
            _mimo_owner_hidden_count = 0
            if _owner_hidden_ids:
                _mimo_inventory = set(
                    _public_mimo_model_ids(_sup, owner, include_owner_hidden=True)
                )
                _mimo_owner_hidden_count = len([
                    model_id
                    for model_id in _owner_hidden_ids
                    if model_id in _mimo_inventory and _is_chat_model(model_id)
                ])
            if _mimo_models:
                _mimo_displays = _mimo_display_names(_base, _variants)
                _mimo_relations = _mimo_model_relations(
                    _sup, owner, _mimo_models
                )
                results.append({
                    "id": "mimo:auto",
                    "name": "Automatic",
                    "base_url": "mimo://acp",
                    "has_key": False,
                    "api_key_fingerprint": None,
                    "is_enabled": True,
                    "models": _mimo_models,
                    "shared_models": [
                        model_id
                        for model_id in _mimo_models
                        if ("native", "mimo:auto", model_id) in shared_routes
                    ],
                    "models_primary": _base,
                    "models_extra": _variants,
                    "catalog": build_model_catalog(
                        endpoint_id="mimo:auto",
                        endpoint_url="mimo://acp",
                        model_ids=_mimo_models,
                        primary_ids=_base,
                        extra_ids=_variants,
                        display_names=_mimo_displays,
                        families=_mimo_model_families(_mimo_models),
                        relations=_mimo_relations,
                        discovered=True,
                        entitled=True,
                        capabilities={"chat": True, "tools": True, "vision": None},
                    ),
                    "pinned_models": [],
                    "hidden_count": _mimo_owner_hidden_count,
                    "online": True,
                    "status": "online",
                    "ping_error": None,
                    "model_type": "llm",
                    "supports_tools": True,
                    "endpoint_kind": "auto",
                    "category": "api",
                    "model_refresh_mode": "auto",
                    "model_refresh_interval": None,
                    "model_refresh_timeout": None,
                    "read_only": True,
                    "selector_only": True,
                })
            for shared in _subscribed_shared_catalog(owner):
                results.append({
                    "id": shared["endpoint_id"],
                    "name": shared["endpoint_name"],
                    "base_url": "mimo://acp",
                    "has_key": False,
                    "api_key_fingerprint": None,
                    "is_enabled": True,
                    "models": list(shared["models"]),
                    "shared_models": [],
                    "models_primary": list(shared["models"]),
                    "models_extra": [],
                    "catalog": shared["catalog"],
                    "pinned_models": [],
                    "hidden_count": 0,
                    "online": True,
                    "status": "online",
                    "ping_error": None,
                    "model_type": "llm",
                    "supports_tools": (
                        shared["catalog"][0].get("capabilities", {}).get("tools")
                        if shared["catalog"]
                        else None
                    ),
                    "endpoint_kind": "proxy",
                    "category": "api",
                    "model_refresh_mode": "auto",
                    "model_refresh_interval": None,
                    "model_refresh_timeout": None,
                    "read_only": True,
                    "selector_only": False,
                    "shared": True,
                    "shared_by": shared["shared_by"],
                })
            return results
        finally:
            db.close()

    @router.post("/model-endpoints")
    def create_model_endpoint(
        request: Request,
        name: str = Form(""),
        base_url: str = Form(...),
        api_key: str = Form(""),
        skip_probe: str = Form("false"),
        require_models: str = Form("false"),
        model_type: str = Form("llm"),
        endpoint_kind: str = Form("auto"),
        model_refresh_mode: str = Form(""),
        model_refresh_interval: str = Form(""),
        model_refresh_timeout: str = Form(""),
        supports_tools: str = Form(""),  # "true"/"false"/"" (unknown)
        pinned_models: str = Form(""),  # admin-pinned IDs: list/JSON/comma/newline
        container_local: str = Form("false"),
    ):
        _owner = _require_endpoint_owner(request)
        _caller = _owner or None
        base_url = canonical_endpoint_base(base_url)
        if not base_url:
            raise HTTPException(400, "Base URL is required")
        _validate_owner_endpoint_url(request, _owner, base_url)
        # Resolve hostname via Tailscale if DNS fails
        from src.endpoint_resolver import resolve_url
        base_url = resolve_url(base_url)
        # In Docker, manually added loopback URLs usually point at a host-local
        # server. Cookbook local serves are launched inside Open Clank itself, so
        # keep those container-local when the frontend marks them as such.
        base_url = _rewrite_loopback_for_docker(base_url, container_local=_truthy(container_local))
        base_url = canonical_endpoint_base(base_url)

        # Auto-generate name from URL if not provided
        if not name.strip():
            name = base_url.replace("http://", "").replace("https://", "").split("/")[0]

        requested_kind = _normalize_endpoint_kind(endpoint_kind)
        refresh_mode = _normalize_endpoint_refresh_mode(model_refresh_mode, requested_kind, base_url)
        refresh_interval = _parse_positive_int(model_refresh_interval, minimum=30, maximum=86400)
        refresh_timeout = _parse_positive_int(model_refresh_timeout, minimum=1, maximum=60)
        require_model_list = _truthy(require_models)
        should_probe = (
            require_model_list or requested_kind in ("api", "proxy") or not _truthy(skip_probe)
        )
        explicit_timeout = _explicit_model_list_timeout(base_url, requested_kind, refresh_timeout)

        # Dedupe only inside this owner's catalogue. Two users may connect the
        # same provider URL with different credentials without sharing a row.
        _incoming_api_key = api_key.strip()
        _db_dedup = SessionLocal()
        try:
            _same_url_query = _db_dedup.query(ModelEndpoint)
            if _caller:
                _same_url_query = _same_url_query.filter(ModelEndpoint.owner == _caller)
            else:
                _same_url_query = _same_url_query.filter(ModelEndpoint.owner.is_(None))
            _same_url_rows = _same_url_query.all()
            existing = matching_endpoint(_same_url_rows, base_url, _incoming_api_key)
            if existing:
                changed = False
                if existing.base_url != base_url:
                    existing.base_url = base_url
                    changed = True
                if not existing.is_enabled:
                    existing.is_enabled = True
                    changed = True
                if _endpoint_refresh_mode(existing) == "disabled":
                    existing.model_refresh_mode = refresh_mode
                    changed = True
                # Persist any incoming pinned IDs onto the existing row. An
                # empty/omitted form field must not wipe previously pinned IDs.
                _incoming_pinned = _normalize_model_ids(pinned_models)
                if _incoming_pinned:
                    _merged_pinned = _merge_model_ids(
                        _normalize_model_ids(getattr(existing, "pinned_models", None)),
                        _incoming_pinned,
                    )
                    existing.pinned_models = json.dumps(_merged_pinned) if _merged_pinned else None
                    changed = True
                existing_kind_for_probe = requested_kind if requested_kind != "auto" else _effective_endpoint_kind(existing, base_url)
                if requested_kind != "auto" and _endpoint_kind(existing) == "auto":
                    existing.endpoint_kind = requested_kind
                    changed = True
                if model_refresh_mode or (requested_kind == "proxy" and _endpoint_refresh_mode(existing, requested_kind) != refresh_mode):
                    existing.model_refresh_mode = refresh_mode
                    changed = True
                if refresh_interval is not None:
                    existing.model_refresh_interval = refresh_interval
                    changed = True
                if refresh_timeout is not None:
                    existing.model_refresh_timeout = refresh_timeout
                    changed = True
                incoming_model_type = (model_type or "").strip() or "llm"
                if incoming_model_type and (getattr(existing, "model_type", None) or "llm") != incoming_model_type:
                    existing.model_type = incoming_model_type
                    changed = True
                if api_key.strip() and not existing.api_key:
                    existing.api_key = api_key.strip()
                    changed = True
                # Keep duplicate endpoint registration cheap. This path is hit
                # by Cookbook/browser auto-register flows and can run while the
                # user is sending a chat message. Probing a stale LAN endpoint
                # here used to hold the request open for tens of seconds and
                # contend with session creation, making "send" feel blocked.
                # Explicit "require models" calls still probe; normal refresh
                # belongs to /model-endpoints/{id}/models or /probe.
                if require_model_list:
                    probed_models, diagnostic = _probe_endpoint_result(
                        base_url,
                        (api_key.strip() or existing.api_key or None),
                        timeout=_explicit_model_list_timeout(base_url, existing_kind_for_probe, refresh_timeout),
                    )
                    _record_catalog_probe(existing, diagnostic)
                    changed = True
                    if probed_models:
                        existing.cached_models = json.dumps(probed_models)
                        changed = True
                # Repair API rows created before pin seeding existed: with no
                # explicit pins they are invisible in the chat picker even
                # though enabled. Rows with a legacy hidden list already render
                # via the legacy fallback, so only heal rows with neither.
                if (
                    not _incoming_pinned
                    and not _has_explicit_pinned_models(existing)
                    and not _hidden_model_ids(existing)
                    and _picker_requires_pinning(base_url, existing_kind_for_probe)
                ):
                    from src.endpoint_resolver import chat_capable_models
                    _seed = chat_capable_models(_cached_model_ids(existing))
                    if _seed:
                        existing.pinned_models = json.dumps(_seed)
                        changed = True
                if changed:
                    _db_dedup.commit()
                    _invalidate_models_cache(_owner)
                    _schedule_mimo_reprojection(request)
                    _local_probe_cache.clear()
                existing_models = _cached_model_ids(existing)
                _existing_pinned = _normalize_model_ids(getattr(existing, "pinned_models", None))
                existing_kind = _effective_endpoint_kind(existing, existing.base_url)
                return {
                    "id": existing.id,
                    "name": existing.name,
                    "base_url": existing.base_url,
                    "has_key": bool(existing.api_key),
                    "api_key_fingerprint": _api_key_fingerprint(existing.api_key),
                    "models": _visible_models(
                        existing_models,
                        getattr(existing, "hidden_models", None),
                        existing.pinned_models,
                    ),
                    "pinned_models": _existing_pinned,
                    "online": True,
                    "status": "online",
                    "existing": True,
                    "is_enabled": bool(existing.is_enabled),
                    "endpoint_kind": existing_kind,
                    "category": _classify_endpoint(existing.base_url, existing_kind),
                    "catalog_probe": _catalog_probe_payload(existing),
                }
        finally:
            _db_dedup.close()

        model_ids, diagnostic = (
            _probe_endpoint_result(
                base_url,
                api_key.strip() or None,
                timeout=explicit_timeout,
            )
            if should_probe
            else ([], {"status": "unknown"})
        )
        ping = {"reachable": False, "error": None}
        if (should_probe or requested_kind in ("api", "proxy")) and not model_ids:
            ping = _ping_endpoint(base_url, api_key.strip() or None, timeout=min(explicit_timeout, 10.0))
        if require_model_list and not model_ids:
            raise HTTPException(400, _model_endpoint_error_message(base_url, ping))

        ep_id = str(uuid.uuid4())[:8]
        db = SessionLocal()
        try:
            _pinned = _normalize_model_ids(pinned_models)
            # API-class pickers treat pinned_models as an allow-list; a row
            # stored without pins is invisible in chat even though it probes
            # fine. Seed every probed chat model so a freshly added connection
            # works immediately (pre-sync behavior: what you add shows up).
            if (
                not _pinned
                and model_ids
                and _picker_requires_pinning(base_url, requested_kind)
            ):
                from src.endpoint_resolver import chat_capable_models
                _pinned = chat_capable_models(model_ids) or []
            ep = ModelEndpoint(
                id=ep_id,
                name=name.strip(),
                base_url=base_url,
                api_key=api_key.strip() or None,
                is_enabled=True,
                model_type=model_type.strip() if model_type else "llm",
                endpoint_kind=requested_kind,
                model_refresh_mode=refresh_mode,
                model_refresh_interval=refresh_interval,
                model_refresh_timeout=refresh_timeout,
                cached_models=json.dumps(model_ids) if model_ids else None,
                pinned_models=json.dumps(_pinned) if _pinned else None,
                # Legacy endpoint-wide authority is intentionally dormant.
                supports_tools=None,
                owner=_caller,
            )
            if should_probe:
                _record_catalog_probe(ep, diagnostic)
            db.add(ep)
            db.commit()
            _schedule_mimo_reprojection(request)
            # Auto-set as default chat endpoint when none is usable yet — either
            # nothing is configured, or the configured default points at an
            # endpoint that is now missing/disabled (#3586). Seed the first CHAT
            # model (not raw model_ids[0]) so we don't pin the global default to
            # an embedding/tts/etc. entry a provider happens to list first.
            if _caller:
                from routes.prefs_routes import _load_for_user, _save_for_user
                settings = _load_for_user(_caller)
            else:
                settings = _load_settings()
            enabled_ids = {
                e.id
                for e in _owned_endpoint_query(db, request).filter(
                    ModelEndpoint.is_enabled == True  # noqa: E712
                ).all()
            }
            current_default_id = settings.get("default_endpoint_id") or ""
            current_default_ep = None
            if current_default_id:
                current_default_ep = _owned_endpoint_query(db, request).filter(
                    ModelEndpoint.id == current_default_id
                ).first()
            if _default_endpoint_needs_assignment(
                current_default_id,
                enabled_ids,
                current_default_endpoint=current_default_ep,
                current_default_model=settings.get("default_model") or "",
            ):
                from src.endpoint_resolver import _first_chat_model
                settings["default_endpoint_id"] = ep.id
                settings["default_model"] = _first_chat_model(model_ids) or ""
                if _caller:
                    _save_for_user(_caller, settings)
                else:
                    _save_settings(settings)
            _invalidate_models_cache(_owner)
            _local_probe_cache.clear()
        finally:
            db.close()

        # Return immediately — probing happens via the separate /probe SSE endpoint
        return {
            "id": ep_id,
            "name": name.strip(),
            "base_url": base_url,
            "has_key": bool(api_key.strip()),
            "api_key_fingerprint": _api_key_fingerprint(api_key),
            "models": _merge_model_ids(model_ids, _pinned),
            "pinned_models": _pinned,
            "online": bool(model_ids) or bool(_pinned) or bool(ping.get("reachable")),
            "status": "online" if (model_ids or _pinned) else ("loading" if ping.get("loading") else ("empty" if ping.get("reachable") else "offline")),
            "is_enabled": True,
            "ping_error": ping.get("error") if ping else None,
            "endpoint_kind": requested_kind,
            "category": _classify_endpoint(base_url, requested_kind),
            "catalog_probe": diagnostic if should_probe else {"status": "unknown"},
        }

    @router.post("/model-endpoints/test")
    def test_model_endpoint(
        request: Request,
        base_url: str = Form(...),
        api_key: str = Form(""),
        endpoint_kind: str = Form("auto"),
        model_refresh_timeout: str = Form(""),
    ):
        owner = _require_endpoint_owner(request)
        base_url = _normalize_base(base_url)
        if not base_url:
            raise HTTPException(400, "Base URL is required")
        _validate_owner_endpoint_url(request, owner, base_url)
        from src.endpoint_resolver import resolve_url
        base_url = resolve_url(base_url)
        base_url = _rewrite_loopback_for_docker(base_url)
        requested_kind = _normalize_endpoint_kind(endpoint_kind)
        configured_timeout = _parse_positive_int(model_refresh_timeout, minimum=1, maximum=60)
        probe_timeout = _explicit_model_list_timeout(base_url, requested_kind, configured_timeout)
        models, diagnostic = _probe_endpoint_result(
            base_url,
            api_key.strip() or None,
            timeout=probe_timeout,
        )
        ping = {"reachable": True, "error": None} if models else _ping_endpoint(base_url, api_key.strip() or None, timeout=min(probe_timeout, 10.0))
        return {
            "base_url": base_url,
            "online": bool(models) or bool(ping.get("reachable")),
            "status": "online" if models else ("loading" if ping.get("loading") else ("empty" if ping.get("reachable") else "offline")),
            "ping_error": ping.get("error") if ping else None,
            "models": models,
            "count": len(models),
            "endpoint_kind": requested_kind,
            "category": _classify_endpoint(base_url, requested_kind),
            "catalog_probe": diagnostic,
        }

    @router.get("/model-endpoints/{ep_id}/probe")
    def probe_endpoint_models(ep_id: str, request: Request):
        """Re-probe all models on an endpoint. Updates hidden_models and streams SSE results."""
        owner = _require_endpoint_owner(request)
        if _mimo_endpoint_provider(ep_id)[0]:
            raise HTTPException(409, "These models refresh automatically with the provider connection")
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(ModelEndpoint.id == ep_id).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
            ep_data = {"id": ep.id, "name": ep.name, "base_url": ep.base_url, "api_key": ep.api_key}
        finally:
            db.close()

        base = _normalize_base(ep_data["base_url"])
        all_models, diagnostic = _probe_endpoint_result(base, ep_data["api_key"])
        chat_models = [m for m in all_models if _is_chat_model(m)]
        skipped = len(all_models) - len(chat_models)

        def _stream():
            yield f"data: {json.dumps({'type': 'probe_start', 'endpoint': ep_data['name'], 'model_count': len(chat_models), 'skipped': skipped})}\n\n"
            failed = []
            ok_count = 0
            for mid in chat_models:
                result = _probe_single_model(base, ep_data["api_key"], mid, timeout=8, owner=_model_owner(request), endpoint_id=ep_data.get("id", ""))
                result["model"] = mid
                result["type"] = "probe_result"
                result["endpoint"] = ep_data["name"]
                if result["status"] == "ok":
                    ok_count += 1
                else:
                    failed.append(mid)
                yield f"data: {json.dumps(result)}\n\n"

            # Update hidden_models and cached_models in DB
            db2 = SessionLocal()
            try:
                ep_obj = _owned_endpoint_query(db2, request).filter(
                    ModelEndpoint.id == ep_id
                ).first()
                if ep_obj:
                    _record_catalog_probe(ep_obj, diagnostic)
                    ep_obj.hidden_models = json.dumps(failed) if failed else None
                    if all_models:
                        ep_obj.cached_models = json.dumps(all_models)
                    db2.commit()
            finally:
                db2.close()
            _invalidate_models_cache(owner)

            yield f"data: {json.dumps({'type': 'probe_done', 'total': len(all_models), 'ok': ok_count, 'hidden': len(failed)})}\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    @router.get("/model-endpoints/{ep_id}/models")
    def list_endpoint_models(
        ep_id: str,
        request: Request,
        response: Response,
        refresh: bool = False,
        refresh_timeout: Optional[int] = Query(None, ge=1, le=60),
    ):
        """List all discovered models for an endpoint with hidden/visible state."""
        owner = _require_endpoint_owner(request)
        _is_mimo, _mimo_pid = _mimo_endpoint_provider(ep_id)
        if _is_mimo:
            supervisor = getattr(request.app.state, "mimo_supervisor", None)
            ensure_owner_mimo_worker(supervisor, owner)
            # Full inventory with per-model visibility: hiding a model must
            # not erase it from the panel, or it could never be re-enabled.
            models = mimo_connection_model_ids(
                supervisor, owner, ep_id, include_owner_hidden=True
            )
            _hidden = (
                _owner_hidden_mimo_model_ids(owner, _mimo_pid) if _mimo_pid else set()
            )
            return [
                {
                    "id": model_id,
                    "display": model_id.split("/", 1)[-1],
                    "is_hidden": model_id in _hidden,
                    "is_pinned": False,
                    "read_only": not _mimo_pid,
                }
                for model_id in models
            ]
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(ModelEndpoint.id == ep_id).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
            hidden = _hidden_model_ids(ep)
            all_models = _cached_model_ids(ep)
            base = _normalize_base(ep.base_url)
            kind = _effective_endpoint_kind(ep, base)
            picker_requires_pinning = _picker_requires_pinning(base, kind)
            if refresh:
                category = _classify_endpoint(base, kind)
                timeout = _manual_refresh_timeout(ep, category, refresh_timeout)
                try:
                    probed, diagnostic = _probe_endpoint_result(
                        base,
                        ep.api_key,
                        timeout=timeout,
                    )
                    _record_catalog_probe(ep, diagnostic)
                except Exception as exc:
                    logger.warning("Manual model refresh failed for endpoint %s at %s: %s", ep_id, base, exc)
                    probed = []
                    _record_catalog_probe(ep, {"status": "unavailable"})
                if probed:
                    all_models = probed
                    ep.cached_models = json.dumps(all_models)
                    _invalidate_models_cache(owner)
                    response.headers["X-Model-Refresh-Status"] = "refreshed"
                    response.headers["X-Model-Refresh-Count"] = str(len(probed))
                else:
                    response.headers["X-Model-Refresh-Status"] = "failed"
                    response.headers["X-Model-Refresh-Warning"] = "Model refresh failed or returned no models; kept cached models."
                db.commit()
            pinned = _normalize_model_ids(getattr(ep, "pinned_models", None))
            if picker_requires_pinning and not _has_explicit_pinned_models(ep):
                pinned = _legacy_visible_api_models(ep)
            pinned_set = set(pinned)
            return [
                {
                    "id": m,
                    "display": m.split("/")[-1],
                    "is_hidden": m in hidden,
                    "is_pinned": m in pinned_set,
                    "picker_requires_pinning": picker_requires_pinning,
                }
                for m in _merge_model_ids(all_models, pinned)
            ]
        finally:
            db.close()

    @router.patch("/model-endpoints/{ep_id}/models")
    async def update_hidden_models(ep_id: str, request: Request):
        """Bulk update hidden and/or pinned model lists for an endpoint.

        Expects JSON body with optional keys:
          {"hidden": ["model-id-1", ...], "pinned_models": ["deploy-id", ...]}
        Each key is updated only when present, so callers can patch one list
        without clobbering the other.
        """
        owner = _require_endpoint_owner(request)
        _is_mimo_ep, _mimo_provider_id = _mimo_endpoint_provider(ep_id)
        if _is_mimo_ep:
            if not _mimo_provider_id:
                raise HTTPException(
                    409,
                    "Automatic routing has no per-model visibility; "
                    "manage the provider accounts instead",
                )
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("hidden"), list):
                raise HTTPException(
                    400, "Body must be a JSON object with a hidden list of model IDs"
                )
            hidden_count = _set_owner_hidden_mimo_models(
                owner, _mimo_provider_id, body["hidden"]
            )
            _invalidate_models_cache(owner)
            return {"id": ep_id, "hidden_count": hidden_count}
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(ModelEndpoint.id == ep_id).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(400, "Body must be a JSON object")
            if "hidden" in body:
                hidden = body.get("hidden")
                if not isinstance(hidden, list):
                    raise HTTPException(400, "hidden must be a list of model IDs")
                base = _normalize_base(ep.base_url)
                kind = _effective_endpoint_kind(ep, base)
                if _picker_requires_pinning(base, kind):
                    # Compatibility for older/admin UI paths that still submit
                    # the previous hide-list shape. API pickers are allow-lists:
                    # convert "unchecked models" into an explicit pinned list so
                    # Settings summary, /api/models, and chat agree.
                    selected = _visible_models(_cached_model_ids(ep), hidden, None)
                    ep.pinned_models = json.dumps(selected)
                    ep.hidden_models = None
                else:
                    ep.hidden_models = json.dumps(hidden) if hidden else None
            # Accept either "pinned" or "pinned_models" for the manual IDs list.
            if "pinned_models" in body or "pinned" in body:
                pinned = _normalize_model_ids(body.get("pinned_models", body.get("pinned")))
                base = _normalize_base(ep.base_url)
                kind = _effective_endpoint_kind(ep, base)
                if _picker_requires_pinning(base, kind):
                    ep.pinned_models = json.dumps(pinned)
                    ep.hidden_models = None
                else:
                    ep.pinned_models = json.dumps(pinned) if pinned else None
            db.commit()
            _invalidate_models_cache(owner)
            _schedule_mimo_reprojection(request)
            hidden_count = len(json.loads(ep.hidden_models)) if ep.hidden_models else 0
            pinned_count = len(json.loads(ep.pinned_models)) if ep.pinned_models else 0
            return {"id": ep_id, "hidden_count": hidden_count, "pinned_count": pinned_count}
        finally:
            db.close()

    @router.get("/default-chat")
    def get_default_chat(request: Request):
        user = _model_owner(request)
        owner = normalized_provider_owner(user)
        allowed = _allowed_model_ids(request, user)
        try:
            own_routes, shared_routes = list_chat_routes(user)
            routes = [*own_routes, *shared_routes]
            by_route_id = {route.model_route_id: route for route in routes}
            bindings = [
                binding
                for binding in ProviderStore().list_route_bindings(owner=owner)
                if binding.purpose == "chat"
            ]
        except (ProviderStoreError, ChatRouteUnavailable) as exc:
            logger.error("Normalized default chat lookup failed: %s", exc)
            raise HTTPException(503, "Default provider route is unavailable") from exc

        ordered = []
        seen = set()
        for binding in bindings:
            if not binding.enabled:
                continue
            route = by_route_id.get(binding.model_route_id)
            if route is None or route.model_route_id in seen:
                continue
            ordered.append(route)
            seen.add(route.model_route_id)
        ordered.extend(
            route for route in routes if route.model_route_id not in seen
        )
        for route in ordered:
            if allowed is not None and (
                route.provider_model_id not in allowed
                and route.model_route_id not in allowed
            ):
                continue
            return {
                "endpoint_id": route.public_endpoint_id,
                "endpoint_url": MANAGED_ENGINE_PUBLIC_URL,
                "model": route.provider_model_id,
            }
        return {"endpoint_id": "", "endpoint_url": "", "model": ""}

    @router.get("/model-endpoints/{ep_id}/capabilities")
    def get_model_capabilities(ep_id: str, request: Request):
        _require_endpoint_owner(request)
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(ModelEndpoint.id == ep_id).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
            from src.model_capabilities import endpoint_capability_states
            return {"endpoint_id": ep_id, "models": endpoint_capability_states(db, ep)}
        finally:
            db.close()

    @router.patch("/model-endpoints/{ep_id}/capabilities")
    async def declare_model_capability(ep_id: str, request: Request):
        _require_endpoint_owner(request)
        body = await request.json()
        model_id = str(body.get("model_id") or "").strip()
        if not model_id:
            raise HTTPException(400, "model_id is required")
        raw = body.get("tools_declared")
        if not isinstance(raw, bool):
            raise HTTPException(400, "tools_declared must be boolean")
        value = raw
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(ModelEndpoint.id == ep_id).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
            if model_id not in _endpoint_enabled_models(ep):
                raise HTTPException(404, "Model is not enabled on this endpoint")
            from src.model_capabilities import capability_state, set_declared
            row = set_declared(db, ep, model_id, value)
            db.commit()
            _schedule_mimo_reprojection(request)
            return capability_state(ep, model_id, row)
        finally:
            db.close()

    @router.post("/model-endpoints/{ep_id}/capabilities/probe")
    async def probe_model_capability(ep_id: str, request: Request):
        _require_endpoint_owner(request)
        body = await request.json()
        model_id = str(body.get("model_id") or "").strip()
        if not model_id:
            raise HTTPException(400, "model_id is required")
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(ModelEndpoint.id == ep_id).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
            if model_id not in _endpoint_enabled_models(ep):
                raise HTTPException(404, "Model is not enabled on this endpoint")
            from src.endpoint_resolver import resolve_endpoint_runtime
            base, api_key = resolve_endpoint_runtime(ep, owner=getattr(ep, "owner", None))
            result = _probe_single_model(base, api_key, model_id, timeout=10, with_tools=True, owner=_model_owner(request), endpoint_id=ep_id)
            from src.model_capabilities import capability_state, set_verified
            row = set_verified(db, ep, model_id, result.get("tool_support"))
            db.commit()
            return {**capability_state(ep, model_id, row), "probe": result}
        finally:
            db.close()

    @router.patch("/model-endpoints/{ep_id}")
    async def toggle_model_endpoint(ep_id: str, request: Request):
        owner = _require_endpoint_owner(request)
        if _mimo_endpoint_provider(ep_id)[0]:
            raise HTTPException(409, "This provider is managed through its connection in Added Models")
        # Optional JSON body for field-targeted updates. No body → toggle is_enabled (legacy behaviour).
        body: Dict[str, Any] = {}
        try:
            if int(request.headers.get("content-length") or 0) > 0:
                body = await request.json()
                if not isinstance(body, dict):
                    body = {}
        except Exception:
            body = {}
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(ModelEndpoint.id == ep_id).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
            if body:
                if "is_enabled" in body:
                    v_ie = body['is_enabled']
                    ep.is_enabled = v_ie.lower() in ('true', '1', 'yes') if isinstance(v_ie, str) else bool(v_ie)
                if "name" in body and isinstance(body["name"], str):
                    ep.name = body["name"].strip() or ep.name
                if "model_type" in body and isinstance(body["model_type"], str):
                    ep.model_type = body["model_type"].strip() or ep.model_type
                if "pinned_models" in body:
                    _pinned = _normalize_model_ids(body["pinned_models"])
                    _base_for_pins = _normalize_base(ep.base_url)
                    _kind_for_pins = _effective_endpoint_kind(ep, _base_for_pins)
                    if _picker_requires_pinning(_base_for_pins, _kind_for_pins):
                        ep.pinned_models = json.dumps(_pinned)
                        ep.hidden_models = None
                    else:
                        ep.pinned_models = json.dumps(_pinned) if _pinned else None
                if "endpoint_kind" in body:
                    ep.endpoint_kind = _normalize_endpoint_kind(body.get("endpoint_kind"))
                if "model_refresh_mode" in body:
                    ep.model_refresh_mode = _normalize_endpoint_refresh_mode(
                        body.get("model_refresh_mode"),
                        _endpoint_kind(ep),
                        ep.base_url,
                    )
                if "model_refresh_interval" in body:
                    interval = _parse_positive_int(body.get("model_refresh_interval"), minimum=30, maximum=86400)
                    ep.model_refresh_interval = interval
                if "model_refresh_timeout" in body:
                    timeout = _parse_positive_int(body.get("model_refresh_timeout"), minimum=1, maximum=60)
                    ep.model_refresh_timeout = timeout
                # Rotating an API key used to require DELETE+POST, which wiped
                # endpoint_url/model from every session referencing the old base
                # URL. Allow in-place updates so the admin can change the key
                # (or correct a typo'd base URL) without nuking session state.
                if "api_key" in body and isinstance(body["api_key"], str):
                    _new_key = body["api_key"].strip()
                    # Empty string means "clear it" (e.g. local Ollama no longer needs a key).
                    ep.api_key = _new_key or None
                if "base_url" in body and isinstance(body["base_url"], str):
                    _new_base = body["base_url"].strip().rstrip("/")
                    for _suffix in ("/models", "/chat/completions", "/completions", "/v1/messages"):
                        if _new_base.endswith(_suffix):
                            _new_base = _new_base[: -len(_suffix)].rstrip("/")
                    _new_base = _normalize_base(_new_base)
                    if _new_base:
                        _validate_owner_endpoint_url(request, owner, _new_base)
                        ep.base_url = _new_base
            else:
                ep.is_enabled = not ep.is_enabled
            db.commit()
            _invalidate_models_cache(owner)
            _schedule_mimo_reprojection(request)
            _local_probe_cache.clear()
            from src.model_capabilities import endpoint_capability_states
            return {
                "id": ep.id,
                "is_enabled": ep.is_enabled,
                "supports_tools": ep.supports_tools,
                "model_capabilities": endpoint_capability_states(db, ep),
                "catalog_probe": _catalog_probe_payload(ep),
                "name": ep.name,
                "model_type": ep.model_type,
                "base_url": ep.base_url,
                "pinned_models": _normalize_model_ids(getattr(ep, "pinned_models", None)),
                "endpoint_kind": getattr(ep, "endpoint_kind", None) or "auto",
                "model_refresh_mode": getattr(ep, "model_refresh_mode", None) or "auto",
                "model_refresh_interval": getattr(ep, "model_refresh_interval", None),
                "model_refresh_timeout": getattr(ep, "model_refresh_timeout", None),
            }
        finally:
            db.close()

    def _settings_using_endpoint(ep_id: str, request: Request) -> list:
        """Return only the caller's endpoint dependents plus admin globals."""
        owner = _model_owner(request)
        affected = []
        if owner:
            from routes.prefs_routes import _load_for_user

            affected.extend(
                _endpoint_settings_using_endpoint(
                    _load_for_user(owner) or {},
                    ep_id,
                    include_speech=True,
                )
            )
        if not owner or _request_is_admin(request, owner):
            affected.extend(
                _endpoint_settings_using_endpoint(
                    _load_settings(),
                    ep_id,
                    include_speech=True,
                )
            )
        return list(dict.fromkeys(affected))

    def _clear_settings_for_endpoint(
        ep_id: str,
        request: Request,
    ) -> tuple[list, int]:
        """Clear the caller's references and admin-owned global references."""
        owner = _model_owner(request)
        cleared = []
        cleared_user_preferences = 0
        if owner:
            from routes.prefs_routes import _load_for_user, _save_for_user

            prefs = _load_for_user(owner) or {}
            owner_cleared = _clear_endpoint_settings_for_endpoint(
                prefs,
                ep_id,
                include_speech=True,
            )
            if owner_cleared:
                _save_for_user(owner, prefs)
                cleared_user_preferences = 1
                cleared.extend(owner_cleared)
        if not owner or _request_is_admin(request, owner):
            settings = _load_settings()
            global_cleared = _clear_endpoint_settings_for_endpoint(
                settings,
                ep_id,
                include_speech=True,
            )
            if global_cleared:
                _save_settings(settings)
                cleared.extend(global_cleared)
        return list(dict.fromkeys(cleared)), cleared_user_preferences

    def _session_uses_endpoint_url(session_url: str, base_url: str) -> bool:
        if not session_url or not base_url:
            return False
        sess = session_url.rstrip("/")
        base = _normalize_base(base_url).rstrip("/")
        variants = {
            base,
            base + "/chat/completions",
            build_chat_url(base).rstrip("/"),
        }
        return sess in variants or sess.startswith(base + "/")

    def _clear_sessions_for_endpoint(db, base_url: str, owner: str | None) -> int:
        """Drop stored auth for sessions using an endpoint being deleted.

        Keep the session's endpoint URL and model intact. If the admin is
        replacing an endpoint with the same URL, clearing those fields leaves
        the UI looking selected while chat requests arrive with an empty model.
        The chat-time orphan guard still clears truly dead endpoints when no
        matching enabled endpoint exists.
        """
        cleared = 0
        rows = db.query(DbSession).filter(DbSession.endpoint_url.isnot(None))
        if owner:
            rows = rows.filter(DbSession.owner == owner)
        rows = rows.all()
        for row in rows:
            if _session_uses_endpoint_url(row.endpoint_url or "", base_url):
                row.headers = {}
                row.updated_at = datetime.utcnow()
                cleared += 1
        return cleared

    def _clear_loaded_sessions_for_endpoint(base_url: str, owner: str | None) -> int:
        try:
            from src.ai_interaction import get_session_manager
            manager = get_session_manager()
        except Exception:
            manager = None
        if not manager:
            return 0
        cleared = 0
        try:
            for sess in list(getattr(manager, "sessions", {}).values()):
                if owner and getattr(sess, "owner", None) != owner:
                    continue
                if _session_uses_endpoint_url(getattr(sess, "endpoint_url", "") or "", base_url):
                    sess.headers = {}
                    cleared += 1
        except Exception:
            return cleared
        return cleared

    @router.get("/model-endpoints/{ep_id}/dependents")
    def get_endpoint_dependents(ep_id: str, request: Request):
        """Check which settings depend on this endpoint."""
        _require_endpoint_owner(request)
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(
                ModelEndpoint.id == ep_id
            ).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
        finally:
            db.close()
        return {"dependents": _settings_using_endpoint(ep_id, request)}

    @router.delete("/model-endpoints/{ep_id}")
    async def delete_model_endpoint(ep_id: str, request: Request):
        owner = _require_endpoint_owner(request)
        if _mimo_endpoint_provider(ep_id)[0]:
            raise HTTPException(409, "Disconnect this provider instead of deleting it")
        db = SessionLocal()
        try:
            ep = _owned_endpoint_query(db, request).filter(ModelEndpoint.id == ep_id).first()
            if not ep:
                raise HTTPException(404, "Endpoint not found")
            # Clean up any settings that reference this endpoint
            cleared, cleared_user_preferences = _clear_settings_for_endpoint(
                ep_id,
                request,
            )
            cleared_sessions = _clear_sessions_for_endpoint(db, ep.base_url, ep.owner)
            cleared_loaded_sessions = _clear_loaded_sessions_for_endpoint(ep.base_url, ep.owner)
            auth_id = getattr(ep, "provider_auth_id", None)
            shares = db.query(ModelShare).filter(
                ModelShare.owner == owner,
                ModelShare.source_kind == "endpoint",
                ModelShare.source_id == ep_id,
            ).all()
            share_ids = [row.id for row in shares]
            share_models = {row.id: row.model_id for row in shares}
            revocations = []
            if share_ids:
                revocations = [
                    (grant.subscriber, grant.share_id, share_models[grant.share_id])
                    for grant in db.query(ModelShareSubscription).filter(
                        ModelShareSubscription.share_id.in_(share_ids),
                        ModelShareSubscription.enabled == True,  # noqa: E712
                    ).all()
                ]
                db.query(ModelShareSubscription).filter(
                    ModelShareSubscription.share_id.in_(share_ids)
                ).delete(synchronize_session=False)
                db.query(ModelShare).filter(
                    ModelShare.id.in_(share_ids)
                ).delete(synchronize_session=False)
            db.delete(ep)
            cleared_provider_auth = _delete_orphaned_provider_auth(db, auth_id, exclude_ep_id=ep_id)
            db.commit()
            _invalidate_models_cache(None if share_ids else owner)
            _schedule_mimo_reprojection(request)
            await revoke_shared_model_workers(request, revocations)
            notify_shared_model_removals(revocations, shared_by=owner)
            _local_probe_cache.clear()
            return {
                "deleted": True,
                "cleared_settings": cleared,
                "cleared_user_preferences": cleared_user_preferences,
                "cleared_sessions": cleared_sessions,
                "cleared_loaded_sessions": cleared_loaded_sessions,
                "cleared_provider_auth": cleared_provider_auth,
                "revoked_model_shares": len(share_ids),
            }
        finally:
            db.close()

    # ── Tool management ──

    @router.get("/tools")
    async def list_tools(request: Request):
        """List all available tools with their enabled/disabled status."""
        from src.agent_tools import TOOL_TAGS
        settings = _load_settings()
        disabled = set(settings.get("disabled_tools", []))
        tools = []
        for tag in sorted(TOOL_TAGS):
            requested = tag not in disabled
            tools.append({
                "id": tag,
                "source": "first-party-adapter",
                "enabled": requested,
                "requested_enabled": requested,
                "effective_enabled": requested,
                "availability": "disabled" if not requested else "available",
                "reason": "Disabled by administrator" if not requested else "Registered first-party adapter",
            })
        native_ids = []
        native_metadata = {}
        native_status = "engine-stopped"
        supervisor = getattr(getattr(request, "app", None), "state", None)
        supervisor = getattr(supervisor, "mimo_supervisor", None)
        try:
            owner = str(effective_user(request) or "").strip().lower()
            worker = supervisor.worker_for_owner(owner) if supervisor is not None else None
            if worker is not None and worker.is_alive():
                async with worker.internal_http_client(timeout=2.0) as client:
                    response = await client.get("/experimental/tool/metadata")
                    if response.is_success:
                        payload = response.json()
                        rows = payload if isinstance(payload, list) else payload.get("tools", [])
                        native_metadata = {str(item.get("id")): item for item in rows if isinstance(item, dict) and str(item.get("id", "")).strip()}
                        native_ids = sorted(native_metadata)
                        native_status = "fresh"
                    else:
                        # Older managed workers expose IDs only; keep their
                        # projection truthful while marking metadata unavailable.
                        response = await client.get("/experimental/tool/ids")
                        if response.is_success:
                            payload = response.json()
                            native_ids = sorted({str(item) for item in (payload if isinstance(payload, list) else payload.get("ids", [])) if str(item).strip()})
                            native_status = "ids-only"
                        else:
                            native_status = "engine-query-failed"
        except Exception:
            native_status = "engine-query-unavailable"
        known = {item["id"] for item in tools}
        for tag in native_ids:
            if tag in known:
                continue
            requested = tag not in disabled
            metadata = native_metadata.get(tag, {})
            registered = metadata.get("registered", True)
            effective = bool(requested and registered)
            tools.append({
                "id": tag,
                "source": metadata.get("source", "native-registry"),
                "enabled": requested,
                "requested_enabled": requested,
                "effective_enabled": effective,
                "availability": "disabled" if not requested else "available" if registered else "unavailable",
                "reason": "Disabled by administrator" if not requested else metadata.get("reason", "Registered by the running native engine") if registered else "Not registered by the running native engine",
            })
        tools.sort(key=lambda item: item["id"])
        return {
            "tools": tools,
            "catalog": {
                "source": "native-registry+first-party-adapter",
                "freshness": native_status,
                "native_registry": "engine-owned; IDs are queried from the running managed engine",
            },
        }

    class ToolsUpdate(BaseModel):
        disabled: list = []

    @router.post("/tools")
    def update_tools(body: ToolsUpdate, request: Request):
        """Update which tools are disabled."""
        require_admin(request)
        settings = _load_settings()
        settings["disabled_tools"] = body.disabled
        _save_settings(settings)
        return {"ok": True, "disabled": body.disabled}

    return router
