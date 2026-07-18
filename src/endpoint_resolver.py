# src/endpoint_resolver.py
"""Unified endpoint resolution for all backend services.

Consolidates the 4+ copies of normalize_base / resolve_endpoint logic into one place.
"""

import json
import logging
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Mapping, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from core.database import SessionLocal, ModelEndpoint
from src.llm_core import _detect_provider, _host_match, _is_kimi_code_url, KIMI_CODE_USER_AGENT, _ollama_api_root

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedModelTarget:
    """A dispatch-ready model selection independent of URL conventions."""

    transport: Literal["http", "acp"]
    endpoint_url: str
    model_id: str
    endpoint_id: Optional[str] = None
    provider_id: Optional[str] = None
    variant_id: Optional[str] = None
    headers: Mapping[str, str] = field(default_factory=dict)
    capabilities: Mapping[str, Optional[bool]] = field(default_factory=dict)
    owner_eligible: bool = True
    supports_stream: bool = True
    lifecycle: Literal["persistent", "ephemeral"] = "persistent"

    def require(self, capability: str) -> None:
        if self.capabilities.get(capability) is not True:
            raise ValueError(
                f"Model {self.model_id!r} does not advertise the {capability!r} capability"
            )


def resolve_model_target(
    endpoint_url: str,
    model: str,
    headers: Optional[Mapping[str, str]] = None,
    *,
    endpoint_id: Optional[str] = None,
    provider_id: Optional[str] = None,
    variant_id: Optional[str] = None,
    capabilities: Optional[Mapping[str, Optional[bool]]] = None,
    owner_eligible: bool = True,
    lifecycle: Literal["persistent", "ephemeral"] = "persistent",
) -> ResolvedModelTarget:
    """Resolve a wire endpoint into the only transport types callers may dispatch."""
    url = (endpoint_url or "").strip()
    model_id = (model or "").strip()
    if not url:
        raise ValueError("No model endpoint is configured")
    if not model_id:
        raise ValueError("No model is selected")
    if not owner_eligible:
        raise PermissionError(f"Model {model_id!r} is not available to this account")

    scheme = urlparse(url).scheme.lower()
    if scheme == "mimo":
        if url.rstrip("/") != "mimo://acp":
            raise ValueError(f"Unsupported MiMo target: {url!r}")
        transport: Literal["http", "acp"] = "acp"
        defaults: Dict[str, Optional[bool]] = {
            "chat": True,
            "tools": True,
            "stream": True,
            "auxiliary": True,
            "vision": None,
        }
        resolved_endpoint_id = endpoint_id or "mimo"
        resolved_provider_id = provider_id or "mimo"
    elif scheme in {"http", "https"}:
        transport = "http"
        defaults = {
            "chat": True,
            "tools": None,
            "stream": True,
            "auxiliary": True,
            "vision": None,
        }
        resolved_endpoint_id = endpoint_id
        resolved_provider_id = provider_id or _detect_provider(url)
    else:
        raise ValueError(f"Unsupported model transport scheme: {scheme or '(missing)'}")

    defaults.update(dict(capabilities or {}))
    return ResolvedModelTarget(
        transport=transport,
        endpoint_url=url,
        model_id=model_id,
        endpoint_id=resolved_endpoint_id,
        provider_id=resolved_provider_id,
        variant_id=variant_id,
        headers=dict(headers or {}),
        capabilities=defaults,
        owner_eligible=True,
        supports_stream=defaults.get("stream") is True,
        lifecycle=lifecycle,
    )

# Model-name substrings that are NOT chat/generation models. When an endpoint
# has no explicit model configured we pick the first CHAT model from its list —
# never an embedding/tts/etc. (an OpenAI-style endpoint often lists
# `text-embedding-ada-002` first, which silently broke email-summarize and
# other resolve_endpoint callers with "Cannot reach model").
_NON_CHAT_MODEL = (
    "text-embedding", "embedding", "tts-", "whisper", "dall-e",
    "moderation", "rerank", "reranker", "clip", "stable-diffusion",
)


def _first_chat_model(models) -> Optional[str]:
    """First model that isn't an embedding/tts/etc.; falls back to models[0]."""
    for m in (models or []):
        if not any(p in str(m).lower() for p in _NON_CHAT_MODEL):
            return m
    return (models[0] if models else None)


def _endpoint_cached_models(ep) -> list:
    """Return cached model ids from the current or legacy endpoint field."""
    raw = getattr(ep, "cached_models", None) or getattr(ep, "models", None)
    if not raw:
        return []
    try:
        models = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return models if isinstance(models, list) else []


def _endpoint_pinned_models(ep) -> list:
    raw = getattr(ep, "pinned_models", None)
    if not raw:
        return []
    try:
        models = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return models if isinstance(models, list) else []


def _is_mlx_deepseek_v4_repo_id(model_id: str) -> bool:
    return "mlx-community/deepseek-v4" in str(model_id or "").lower()


def _is_mlx_deepseek_v4_shim_id(model_id: str) -> bool:
    return "/.cache/odysseus/mlx-shims/deepseek-v4" in str(model_id or "").lower()


def _filter_mlx_deepseek_v4_repo_when_shimmed(model_ids) -> list:
    ids = list(model_ids or [])
    has_shim = any(_is_mlx_deepseek_v4_shim_id(m) for m in ids)
    if not has_shim:
        return ids
    return [m for m in ids if not _is_mlx_deepseek_v4_repo_id(m)]


def _endpoint_hidden_models(ep) -> set:
    """Model ids the admin disabled on this endpoint (the UI's hidden list)."""
    raw = getattr(ep, "hidden_models", None)
    if not raw:
        return set()
    try:
        hidden = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return set()
    return set(hidden) if isinstance(hidden, list) else set()


def _endpoint_enabled_models(ep) -> list:
    """Cached models minus the ones disabled on the endpoint, order preserved.

    The auto-pick fallback must never select a model the user disabled — a
    Groq endpoint can list 16 models with only 1 enabled, and picking the
    raw first one resolves to a model that 400s ("requires terms acceptance").
    """
    hidden = _endpoint_hidden_models(ep)
    merged = []
    seen = set()
    for m in [*_endpoint_cached_models(ep), *_endpoint_pinned_models(ep)]:
        if not isinstance(m, str) or not m or m in seen:
            continue
        seen.add(m)
        merged.append(m)
    merged = _filter_mlx_deepseek_v4_repo_when_shimmed(merged)
    return [m for m in merged if m not in hidden]


def resolve_endpoint_runtime(ep, owner: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Resolve a ModelEndpoint row to its runtime base URL and bearer/API key.

    Static-key providers use ``ModelEndpoint.api_key``. Session-backed providers
    store refreshable credentials in ProviderAuthSession and must resolve a
    current access token at call time.
    """
    base = normalize_base(getattr(ep, "base_url", "") or "")
    api_key = getattr(ep, "api_key", None)
    auth_id = getattr(ep, "provider_auth_id", None)
    if auth_id:
        from src.chatgpt_subscription import resolve_runtime_credentials

        creds = resolve_runtime_credentials(auth_id, owner=owner)
        base = normalize_base(creds.get("base_url") or base)
        api_key = creds.get("api_key")
    return base, api_key


# Cache for Tailscale hostname → IP resolution
_tailscale_cache: Dict[str, Optional[str]] = {}


def _resolve_tailscale_host(hostname: str) -> Optional[str]:
    """Try to resolve a hostname via 'tailscale status' if DNS fails."""
    if hostname in _tailscale_cache:
        return _tailscale_cache[hostname]

    # First check if normal DNS works
    try:
        socket.getaddrinfo(hostname, None, socket.AF_INET)
        _tailscale_cache[hostname] = None  # DNS works, no override needed
        return None
    except socket.gaierror:
        pass

    # DNS failed — try tailscale
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            import json as _json
            data = _json.loads(result.stdout)
            peers = data.get("Peer", {})
            for _id, peer in peers.items():
                peer_name = (peer.get("HostName") or "").lower()
                dns_name = (peer.get("DNSName") or "").split(".")[0].lower()
                if peer_name == hostname.lower() or dns_name == hostname.lower():
                    addrs = peer.get("TailscaleIPs", [])
                    if addrs:
                        ip = addrs[0]
                        logger.info(f"Resolved '{hostname}' via Tailscale → {ip}")
                        _tailscale_cache[hostname] = ip
                        return ip
    except Exception as e:
        logger.debug(f"Tailscale resolution failed for '{hostname}': {e}")

    _tailscale_cache[hostname] = None
    return None


def resolve_url(url: str) -> str:
    """If a URL's hostname can't be resolved via DNS, try Tailscale."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return url
    ip = _resolve_tailscale_host(hostname)
    if ip:
        # Replace hostname with IP in the URL
        netloc = ip
        if parsed.port:
            netloc = f"{ip}:{parsed.port}"
        return urlunparse(parsed._replace(netloc=netloc))
    return url


def normalize_base(url: str) -> str:
    """Strip known API path suffixes from a base URL."""
    url = (url or "").strip().rstrip("/")
    for suffix in ["/models", "/chat/completions", "/completions", "/v1/messages", "/responses"]:
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    for suffix in ["/chat", "/tags", "/generate"]:
        if url.endswith("/api" + suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


def _validated_endpoint_base(url: str) -> str:
    """Return a base URL that is safe for endpoint path appends."""
    base = (url or "").strip().rstrip("/")
    if "?" in base or "#" in base:
        raise ValueError("Endpoint base URL must not include query or fragment")
    parsed = urlparse(base)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("HTTP endpoint base must use http or https")
    return urlunparse(parsed._replace(query="", fragment="")).rstrip("/")


def _prepare_endpoint_base(base: str) -> str:
    base = _validated_endpoint_base(normalize_base(base))
    return _validated_endpoint_base(normalize_base(resolve_url(base)))


def _append_endpoint_path(base: str, suffix: str) -> str:
    parsed = urlparse(base)
    current = (parsed.path or "").rstrip("/")
    extra = "/" + suffix.lstrip("/")
    path = f"{current}{extra}" if current else extra
    return urlunparse(parsed._replace(path=path, query="", fragment=""))


def _pathless_host(base: str, host: str) -> bool:
    parsed = urlparse(base)
    return (parsed.hostname or "").lower() == host and not (parsed.path or "").strip("/")


def _anthropic_api_root(base: str) -> str:
    """Return Anthropic's API root, preserving /v1 for OpenAI-compatible APIs elsewhere."""
    base = (base or "").strip().rstrip("/")
    if _host_match(base, "anthropic.com") and base.endswith("/v1"):
        return base[:-3].rstrip("/")
    return base


def build_chat_url(base: str) -> str:
    """Return the correct chat endpoint URL for a given base."""
    base = _prepare_endpoint_base(base)
    provider = _detect_provider(base)
    if provider == "anthropic":
        return _append_endpoint_path(_anthropic_api_root(base), "/v1/messages")
    if provider == "ollama":
        return _append_endpoint_path(_ollama_api_root(base), "/chat")
    if provider == "chatgpt-subscription":
        return _append_endpoint_path(base, "/responses")
    if _pathless_host(base, "api.openai.com"):
        base = _append_endpoint_path(base, "/v1")
    return _append_endpoint_path(base, "/chat/completions")


def build_models_url(base: str) -> Optional[str]:
    """Return the provider-specific model-list endpoint URL for a base.

    For OpenAI-compatible servers (LM Studio, llama.cpp, vLLM,
    text-generation-webui, etc.) the model list is exposed at ``/v1/models``.
    When the user-supplied base has no path — e.g. ``http://localhost:1234`` —
    we still need to land on ``/v1/models`` (issue #25); insert the ``/v1``
    segment only when the path is empty, leaving any explicit non-empty path
    untouched (so custom prefixes like ``/openai`` or ``/api/openai/v1`` keep
    their semantics).
    """
    base = _prepare_endpoint_base(base)
    provider = _detect_provider(base)
    if provider == "anthropic":
        return _append_endpoint_path(_anthropic_api_root(base), "/v1/models")
    if provider == "ollama":
        return _append_endpoint_path(_ollama_api_root(base), "/tags")
    if provider == "chatgpt-subscription":
        return None
    # Generic OpenAI-compatible fallback: local model servers with no explicit
    # path conventionally expose `/v1/models` (LM Studio, llama.cpp, vLLM).
    # For non-local unknown hosts, do not invent `/v1`; append `/models` to the
    # caller's base so look-alike provider hosts stay generic.
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    is_local = host in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
    uses_v1_models_by_default = is_local or host in {"api.deepseek.com", "api.openai.com"}
    if not parsed.path and uses_v1_models_by_default:
        base = _append_endpoint_path(base, "/v1")
    return _append_endpoint_path(base, "/models")


def build_headers(api_key: Optional[str], base: str) -> Dict[str, str]:
    """Build auth headers for an endpoint."""
    provider = _detect_provider(base)
    headers: Dict[str, str] = {}
    if provider == "anthropic":
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        return headers
    if provider == "copilot":
        from src.copilot import copilot_headers
        return copilot_headers(api_key)
    if provider == "chatgpt-subscription":
        from src.chatgpt_subscription import chatgpt_headers
        return chatgpt_headers(api_key)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if provider == "openrouter":
        headers.setdefault("HTTP-Referer", "https://github.com/pewdiepie-archdaemon/odysseus")
        headers.setdefault("X-OpenRouter-Title", "Odysseus")
    if _is_kimi_code_url(base):
        headers.setdefault("User-Agent", KIMI_CODE_USER_AGENT)
    return headers


def resolve_endpoint(
    setting_prefix: str,
    fallback_url: Optional[str] = None,
    fallback_model: Optional[str] = None,
    fallback_headers: Optional[Dict] = None,
    owner: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], Optional[Dict]]:
    """Resolve an endpoint/model from settings, with fallback.

    Args:
        setting_prefix: Settings key prefix, e.g. "research", "task", "utility", "default".
                       Reads ``{prefix}_endpoint_id`` and ``{prefix}_model`` from settings.
        fallback_url:    URL to use if settings are empty or endpoint missing.
        fallback_model:  Model to use if settings are empty.
        fallback_headers: Headers to use if using fallback.

    Returns:
        (endpoint_url, model, headers) — resolved or fallback values.
    """
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
    except Exception:
        return fallback_url, fallback_model, fallback_headers

    owner_str = owner or ""
    def _stg(key: str) -> str:
        return (get_user_setting(key, owner_str, settings.get(key, "")) or "").strip()

    ep_id = _stg(f"{setting_prefix}_endpoint_id")
    model = _stg(f"{setting_prefix}_model")

    # Fall back to utility model for task/research/auto-naming if not specifically configured.
    if not ep_id and setting_prefix not in ("utility", "default"):
        ep_id = _stg("utility_endpoint_id")
        model = _stg("utility_model")

    # If the endpoint is STILL not configured, but the caller provided a
    # valid fallback (e.g. the active session model), use that immediately.
    # This prevents background tasks from jumping to the global default_model
    # when the user is mid-conversation with a different model.
    if not ep_id and fallback_url and fallback_model:
        return fallback_url, fallback_model, fallback_headers

    # Unset Utility (or anything else that didn't have a fallback) means "same as Default Chat Model".
    if not ep_id:
        ep_id = _stg("default_endpoint_id")
        model = _stg("default_model")

    if not ep_id:
        return fallback_url, fallback_model, fallback_headers

    if ep_id == "mimo" or ep_id.startswith("mimo:"):
        return "mimo://acp", model or fallback_model, fallback_headers or {}

    db = SessionLocal()
    try:
        ep = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == ep_id,
            ModelEndpoint.is_enabled == True,
        )
        if owner:
            from src.auth_helpers import owner_filter
            ep = owner_filter(ep, ModelEndpoint, owner).first()
        else:
            ep = ep.first()
        if not ep:
            return fallback_url, fallback_model, fallback_headers

        try:
            base, api_key = resolve_endpoint_runtime(ep, owner=owner)
        except Exception as e:
            logger.warning("Could not resolve endpoint runtime credentials: %s", e)
            return fallback_url, fallback_model, fallback_headers
        chat_url = build_chat_url(base)
        headers = build_headers(api_key, base)

        # Discard a configured model the user has since disabled on the
        # endpoint (e.g. a stale `default_model` left pointing at a now-hidden
        # model). Treat it as unset so the picker below selects a live one
        # instead of dispatching to a disabled model that 400s.
        if model and model in _endpoint_hidden_models(ep):
            model = ""
        # If no (usable) model specified, pick the first enabled chat model.
        if not model:
            model = _first_chat_model(_endpoint_enabled_models(ep)) or ""
        if not model and not fallback_model:
            logger.warning('[resolve_endpoint] no usable model (all models hidden or list empty)')

        return chat_url, model or fallback_model, headers
    except Exception as e:
        logger.debug(f"Could not resolve {setting_prefix} endpoint: {e}")
        return fallback_url, fallback_model, fallback_headers
    finally:
        db.close()


def endpoint_id_for_chat_url(chat_url: str, owner: Optional[str] = None) -> Optional[str]:
    """Reverse-map a session's chat URL to its ModelEndpoint id.

    Sessions store the fully built chat URL; the registry stores base
    URLs. Needed by the mimo-drives-agent dispatch to find which
    projected provider (`ody-<endpoint_id>`) serves a session's model.
    First enabled match wins; None when the URL isn't registry-backed
    (direct URLs, mimo://acp, deleted endpoints)."""
    wanted = (chat_url or "").strip().rstrip("/")
    if not wanted.startswith(("http://", "https://")):
        return None
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        for ep in q.all():
            base = normalize_base(getattr(ep, "base_url", "") or "")
            if not base:
                continue
            if build_chat_url(base).rstrip("/") == wanted or base.rstrip("/") == wanted:
                return ep.id
        return None
    except Exception as exc:
        logger.debug("endpoint reverse lookup failed for %s: %s", chat_url, exc)
        return None
    finally:
        db.close()


def resolve_endpoint_by_id(
    ep_id: str, model: Optional[str] = None, owner: Optional[str] = None
) -> Optional[Tuple[str, str, Dict]]:
    """Resolve a specific endpoint id (+ optional model) to (chat_url, model, headers).

    Returns None if the endpoint doesn't exist or is disabled. Used to turn
    a configured fallback entry ({endpoint_id, model}) into a dispatch target.
    """
    if not ep_id:
        return None
    if ep_id == "mimo" or ep_id.startswith("mimo:"):
        provider_prefix = ep_id.split(":", 1)[1] if ":" in ep_id else ""
        selected = (model or "").strip()
        try:
            from src.model_dispatch import get_mimo_supervisor

            supervisor = get_mimo_supervisor()
            try:
                catalog = supervisor.available_models(owner=owner) if supervisor else []
            except TypeError:
                catalog = supervisor.available_models() if supervisor else []
            available = [
                item.get("modelId")
                for item in catalog
                if item.get("modelId")
            ]
            if provider_prefix:
                available = [
                    mid for mid in available
                    if mid.split("/", 1)[0] == provider_prefix
                ]
            if available:
                if selected and selected not in available:
                    return None
                selected = selected or available[0]
        except Exception:
            pass
        if not selected:
            return None
        return "mimo://acp", selected, {}
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(
            ModelEndpoint.id == ep_id,
            ModelEndpoint.is_enabled == True,
        )
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        ep = q.first()
        if not ep:
            return None
        try:
            base, api_key = resolve_endpoint_runtime(ep, owner=owner)
        except Exception as e:
            logger.warning("Could not resolve endpoint runtime credentials: %s", e)
            return None
        chat_url = build_chat_url(base)
        headers = build_headers(api_key, base)
        m = (model or "").strip()
        # Drop a model the user disabled on the endpoint, then pick the first
        # enabled chat model rather than a hidden one.
        if m and m in _endpoint_hidden_models(ep):
            m = ""
        if not m:
            m = _first_chat_model(_endpoint_enabled_models(ep)) or ""
        if not m:
            return None
        return chat_url, m, headers
    except Exception as e:
        logger.debug(f"Could not resolve endpoint {ep_id}: {e}")
        return None
    finally:
        db.close()


def resolve_chat_fallback_candidates(owner: Optional[str] = None) -> list:
    """Build the configured default-chat fallback chain as a list of
    (chat_url, model, headers) tuples, skipping any that can't resolve.

    The primary model is NOT included — callers prepend their session's
    current (url, model, headers) so per-session model overrides are honored.
    """
    return _resolve_fallback_candidates("default_model_fallbacks", owner=owner)


def resolve_utility_fallback_candidates(owner: Optional[str] = None) -> list:
    """Configured fallback chain for the Utility model (`utility_model_fallbacks`)."""
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        utility_ep = (get_user_setting("utility_endpoint_id", owner or "", settings.get("utility_endpoint_id", "")) or "").strip()
        if not utility_ep:
            utility_chain = get_user_setting("utility_model_fallbacks", owner or "", settings.get("utility_model_fallbacks") or []) or []
            if utility_chain:
                return _resolve_fallback_candidates("utility_model_fallbacks", owner=owner)
            return _resolve_fallback_candidates("default_model_fallbacks", owner=owner)
    except Exception:
        pass
    return _resolve_fallback_candidates("utility_model_fallbacks", owner=owner)


def resolve_vision_fallback_candidates(owner: Optional[str] = None) -> list:
    """Configured fallback chain for the Vision model (`vision_model_fallbacks`)."""
    return _resolve_fallback_candidates("vision_model_fallbacks", owner=owner)


def _resolve_fallback_candidates(setting_key: str, owner: Optional[str] = None) -> list:
    out = []
    try:
        from src.settings import get_user_setting, load_settings
        settings = load_settings()
        chain = get_user_setting(setting_key, owner or "", settings.get(setting_key) or []) or []
    except Exception:
        return out
    for entry in chain:
        if not isinstance(entry, dict):
            continue
        resolved = resolve_endpoint_by_id(entry.get("endpoint_id", ""), entry.get("model", ""), owner=owner)
        if resolved:
            out.append(resolved)
    return out
