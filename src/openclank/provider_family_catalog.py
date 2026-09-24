"""Deterministic, supervisor-free provider-family catalogue.

The settings UI needs family metadata before any provider connection exists.
Serving that metadata through the managed engine made a read capable of
starting an owner worker and waiting for its cold-start catalogue probe.  This
module instead ships the reviewed public projection produced from the pinned
engine/model inputs.  ``generate_provider_family_catalog`` is the parity
checker used by tests and release tooling; normal request handling only copies
the already-materialized bundle below and performs no filesystem, database,
network, or process work.

OAuth methods are deliberately absent from the bundle.  They are discovered
from the engine's plugin registry and cannot be inferred truthfully from the
pinned model snapshot.  The response marks that enrichment as deferred while
retaining the static API-key and keyless methods defined by the managed
provider contract.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from src.runtime_paths import get_app_root


class ProviderFamilyCatalogError(RuntimeError):
    """The bundled catalogue or its pinned source contract is invalid."""


_CATALOG_SCHEMA_VERSION = 1
_FAMILY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MODEL_CATALOG_SHA256 = "33f836f532fd8ada58f255f11030ff5500d45b0cf7e63587772221050ffb1f48"
_ENGINE_SOURCE_SHA256 = "16ded413b74e82905dd4846e780e9127624435e610c3d404bc900a0d01a2a91a"
_MANAGED_SCHEMA_VERSION = 2
_MANAGED_SCHEMA_SHA256 = "e21dac877632b285e84fb84e7921d8c1c084e6002de25e2d59cf5da6f43dbb50"
_MANAGED_ADAPTERS = {
    "@ai-sdk/openai-compatible": "models-dev-openai-compatible",
    "@ai-sdk/anthropic": "models-dev-anthropic",
}

# id, display name, adapters, kinds, billing lanes, API key, keyless, count.
# These definitions mirror FAMILY_DEFINITIONS in the pinned managed engine.
_BUILTIN_FAMILIES = (
    ("openai", "OpenAI", ("openai-responses", "openai-chat"), ("official", "subscription", "custom_gateway"), ("metered_api", "subscription", "custom"), True, False, 47),
    ("anthropic", "Anthropic", ("anthropic-messages",), ("official", "subscription", "custom_gateway"), ("metered_api", "subscription", "custom"), True, False, 23),
    ("github-copilot", "GitHub Copilot", ("copilot-chat",), ("subscription",), ("subscription",), False, False, 25),
    ("xiaomi", "Xiaomi", ("mimo-native",), ("official", "subscription"), ("metered_api", "subscription"), True, False, 3),
    ("google", "Google", ("google-generative-ai", "google-vertex"), ("official", "custom_gateway"), ("metered_api", "custom"), True, False, 35),
    ("xai", "xAI", ("xai-responses",), ("official", "subscription", "custom_gateway"), ("metered_api", "subscription", "custom"), True, False, 25),
    ("openrouter", "OpenRouter", ("openai-chat",), ("official", "custom_gateway"), ("metered_api", "custom"), True, False, 166),
    ("deepseek", "DeepSeek", ("openai-chat",), ("official",), ("metered_api",), True, False, 2),
    ("ollama", "Ollama", ("ollama",), ("local",), ("local",), True, True, 0),
    ("openai-compatible", "OpenAI-compatible gateway", ("openai-chat", "openai-responses"), ("custom_gateway", "local"), ("custom", "local"), True, True, 0),
    ("local-executor", "Open Clank local executor", ("openclank-local-executor",), ("local",), ("local",), False, True, 0),
)

# Generated from the model-catalogue hash above.  Dynamic models.dev families
# intentionally expose only the two adapter packages admitted by the managed
# engine.  Their auth contract is API-key-only and therefore complete without
# engine plugin discovery.
_MODELS_DEV_FAMILIES = (
    ("302ai", "302.AI", "models-dev-openai-compatible", 64),
    ("abacus", "Abacus", "models-dev-openai-compatible", 65),
    ("aihubmix", "AIHubMix", "models-dev-openai-compatible", 48),
    ("alibaba", "Alibaba", "models-dev-openai-compatible", 41),
    ("alibaba-cn", "Alibaba (China)", "models-dev-openai-compatible", 74),
    ("alibaba-coding-plan", "Alibaba Coding Plan", "models-dev-openai-compatible", 8),
    ("alibaba-coding-plan-cn", "Alibaba Coding Plan (China)", "models-dev-openai-compatible", 8),
    ("bailing", "Bailing", "models-dev-openai-compatible", 2),
    ("baseten", "Baseten", "models-dev-openai-compatible", 12),
    ("berget", "Berget.AI", "models-dev-openai-compatible", 8),
    ("chutes", "Chutes", "models-dev-openai-compatible", 68),
    ("clarifai", "Clarifai", "models-dev-openai-compatible", 11),
    ("cloudferro-sherlock", "CloudFerro Sherlock", "models-dev-openai-compatible", 5),
    ("cortecs", "Cortecs", "models-dev-openai-compatible", 28),
    ("drun", "D.Run (China)", "models-dev-openai-compatible", 3),
    ("dinference", "DInference", "models-dev-openai-compatible", 3),
    ("evroc", "evroc", "models-dev-openai-compatible", 13),
    ("fastrouter", "FastRouter", "models-dev-openai-compatible", 15),
    ("fireworks-ai", "Fireworks AI", "models-dev-openai-compatible", 14),
    ("firmware", "Firmware", "models-dev-openai-compatible", 24),
    ("friendli", "Friendli", "models-dev-openai-compatible", 7),
    ("github-models", "GitHub Models", "models-dev-openai-compatible", 55),
    ("helicone", "Helicone", "models-dev-openai-compatible", 91),
    ("huggingface", "Hugging Face", "models-dev-openai-compatible", 20),
    ("iflowcn", "iFlow", "models-dev-openai-compatible", 14),
    ("inception", "Inception", "models-dev-openai-compatible", 4),
    ("inference", "Inference", "models-dev-openai-compatible", 9),
    ("io-net", "IO.NET", "models-dev-openai-compatible", 17),
    ("jiekou", "Jiekou.AI", "models-dev-openai-compatible", 61),
    ("kilo", "Kilo Gateway", "models-dev-openai-compatible", 335),
    ("kimi-for-coding", "Kimi For Coding", "models-dev-anthropic", 2),
    ("kuae-cloud-coding-plan", "KUAE Cloud Coding Plan", "models-dev-openai-compatible", 1),
    ("llama", "Llama", "models-dev-openai-compatible", 7),
    ("llmgateway", "LLM Gateway", "models-dev-openai-compatible", 203),
    ("lucidquery", "LucidQuery AI", "models-dev-openai-compatible", 2),
    ("meganova", "Meganova", "models-dev-openai-compatible", 19),
    ("minimax", "MiniMax (minimax.io)", "models-dev-anthropic", 6),
    ("minimax-cn", "MiniMax (minimaxi.com)", "models-dev-anthropic", 6),
    ("minimax-coding-plan", "MiniMax Coding Plan (minimax.io)", "models-dev-anthropic", 6),
    ("minimax-cn-coding-plan", "MiniMax Coding Plan (minimaxi.com)", "models-dev-anthropic", 6),
    ("moark", "Moark", "models-dev-openai-compatible", 2),
    ("modelscope", "ModelScope", "models-dev-openai-compatible", 7),
    ("moonshotai", "Moonshot AI", "models-dev-openai-compatible", 6),
    ("moonshotai-cn", "Moonshot AI (China)", "models-dev-openai-compatible", 6),
    ("morph", "Morph", "models-dev-openai-compatible", 3),
    ("nano-gpt", "NanoGPT", "models-dev-openai-compatible", 519),
    ("nebius", "Nebius Token Factory", "models-dev-openai-compatible", 49),
    ("nova", "Nova", "models-dev-openai-compatible", 2),
    ("novita-ai", "NovitaAI", "models-dev-openai-compatible", 84),
    ("nvidia", "Nvidia", "models-dev-openai-compatible", 74),
    ("ollama-cloud", "Ollama Cloud", "models-dev-openai-compatible", 34),
    ("ovhcloud", "OVHcloud AI Endpoints", "models-dev-openai-compatible", 13),
    ("poe", "Poe", "models-dev-openai-compatible", 124),
    ("qihang-ai", "QiHang", "models-dev-openai-compatible", 9),
    ("qiniu-ai", "Qiniu", "models-dev-openai-compatible", 91),
    ("requesty", "Requesty", "models-dev-openai-compatible", 38),
    ("scaleway", "Scaleway", "models-dev-openai-compatible", 16),
    ("siliconflow", "SiliconFlow", "models-dev-openai-compatible", 71),
    ("siliconflow-cn", "SiliconFlow (China)", "models-dev-openai-compatible", 78),
    ("stackit", "STACKIT", "models-dev-openai-compatible", 8),
    ("stepfun", "StepFun", "models-dev-openai-compatible", 3),
    ("submodel", "submodel", "models-dev-openai-compatible", 9),
    ("synthetic", "Synthetic", "models-dev-openai-compatible", 28),
    ("tencent-coding-plan", "Tencent Coding Plan (China)", "models-dev-openai-compatible", 8),
    ("upstage", "Upstage", "models-dev-openai-compatible", 3),
    ("vultr", "Vultr", "models-dev-openai-compatible", 4),
    ("wandb", "Weights & Biases", "models-dev-openai-compatible", 17),
    ("zai", "Z.AI", "models-dev-openai-compatible", 11),
    ("zai-coding-plan", "Z.AI Coding Plan", "models-dev-openai-compatible", 12),
    ("zhipuai", "Zhipu AI", "models-dev-openai-compatible", 10),
    ("zhipuai-coding-plan", "Zhipu AI Coding Plan", "models-dev-openai-compatible", 13),
)


def _auth_methods(*, api_key: bool, keyless: bool) -> list[dict[str, str]]:
    methods: list[dict[str, str]] = []
    if api_key:
        methods.append({"id": "api_key", "type": "api", "label": "API key"})
    if keyless:
        methods.append({"id": "none", "type": "none", "label": "No credential"})
    return methods


def _catalog_identity() -> dict[str, Any]:
    return {
        "catalog_schema_version": _CATALOG_SCHEMA_VERSION,
        "managed_acp_version": 1,
        "managed_schema_version": _MANAGED_SCHEMA_VERSION,
        "managed_schema_sha256": _MANAGED_SCHEMA_SHA256,
        "model_catalog_sha256": _MODEL_CATALOG_SHA256,
        "engine_source_sha256": _ENGINE_SOURCE_SHA256,
    }


def _catalog_key(identity: Mapping[str, Any]) -> str:
    material = json.dumps(
        dict(identity),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "pfcat_" + hashlib.sha256(material).hexdigest()


def _payload(
    builtin_families: tuple[tuple[Any, ...], ...],
    models_dev_families: tuple[tuple[str, str, str, int], ...],
    *,
    identity: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    families: list[dict[str, Any]] = []
    for (
        family_id,
        display_name,
        adapters,
        kinds,
        billing_lanes,
        api_key,
        keyless,
        model_count,
    ) in builtin_families:
        families.append(
            {
                "id": family_id,
                "display_name": display_name,
                "adapters": list(adapters),
                "kinds": list(kinds),
                "billing_lanes": list(billing_lanes),
                "auth_methods": _auth_methods(api_key=api_key, keyless=keyless),
                "auth_methods_complete": False,
                "model_count": int(model_count),
            }
        )
    for family_id, display_name, adapter_id, model_count in models_dev_families:
        families.append(
            {
                "id": family_id,
                "display_name": display_name,
                "adapters": [adapter_id],
                "kinds": ["official"],
                "billing_lanes": ["metered_api"],
                "auth_methods": _auth_methods(api_key=True, keyless=False),
                "auth_methods_complete": True,
                "model_count": int(model_count),
            }
        )
    catalog_identity = dict(identity or _catalog_identity())
    return {
        "schema_version": _CATALOG_SCHEMA_VERSION,
        "source": "bundled-pinned-model-catalog",
        "catalog_key": _catalog_key(catalog_identity),
        "catalog_identity": catalog_identity,
        "auth_method_enrichment": {
            "status": "deferred",
            "source": "managed-engine",
            "oauth_methods_complete": False,
        },
        "families": families,
    }


_BUNDLED_PAYLOAD = _payload(_BUILTIN_FAMILIES, _MODELS_DEV_FAMILIES)


def bundled_provider_family_catalog() -> dict[str, Any]:
    """Return an isolated copy of the zero-I/O catalogue used by HTTP reads."""

    return deepcopy(_BUNDLED_PAYLOAD)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_vendor_child(vendor_root: Path, relative: Any) -> Path:
    text = str(relative or "")
    if not text or "\x00" in text:
        raise ProviderFamilyCatalogError("model catalogue path is invalid")
    candidate = (vendor_root / text).resolve()
    try:
        candidate.relative_to(vendor_root.resolve())
    except ValueError as exc:
        raise ProviderFamilyCatalogError(
            "model catalogue path escapes the managed-engine bundle"
        ) from exc
    return candidate


def _public_https_url(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw or "${" in raw:
        return False
    try:
        parsed = urlsplit(raw)
        hostname = str(parsed.hostname or "").strip("[]").lower()
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname not in {
            "metadata.google.internal",
            "metadata.goog",
            "kubernetes.default.svc",
        }
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def generate_provider_family_catalog(
    app_root: Path | str | None = None,
) -> dict[str, Any]:
    """Regenerate the host projection from the pinned managed-engine inputs.

    This function is intentionally outside the request path.  It exists so a
    vendor/catalogue update cannot silently leave the portable Python bundle
    stale.
    """

    root = Path(app_root or get_app_root()).resolve()
    vendor_root = root / "packages" / "mimo-code"
    manifest_path = vendor_root / "openclank-vendor.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderFamilyCatalogError(
            "managed-engine vendor manifest is unavailable"
        ) from exc
    try:
        model_contract = manifest["inputs"]["model_catalog"]
        managed_schema = manifest["managed_schema"]
        protocols = manifest["protocols"]
        build = manifest["build"]
        model_path = _safe_vendor_child(vendor_root, model_contract["path"])
        identity = {
            "catalog_schema_version": _CATALOG_SCHEMA_VERSION,
            "managed_acp_version": int(protocols["managed_acp"]),
            "managed_schema_version": int(managed_schema["version"]),
            "managed_schema_sha256": str(managed_schema["sha256"]),
            "model_catalog_sha256": str(model_contract["sha256"]),
            "engine_source_sha256": str(build["source_sha256"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderFamilyCatalogError(
            "managed-engine vendor manifest lacks catalogue identity"
        ) from exc
    if identity != _catalog_identity() or _sha256(model_path) != _MODEL_CATALOG_SHA256:
        raise ProviderFamilyCatalogError(
            "managed-engine catalogue inputs do not match the bundled projection"
        )
    try:
        source_catalog = json.loads(model_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderFamilyCatalogError("pinned model catalogue is unavailable") from exc
    if not isinstance(source_catalog, Mapping):
        raise ProviderFamilyCatalogError("pinned model catalogue must be an object")

    builtin_rows: list[tuple[Any, ...]] = []
    builtin_by_id = {str(row[0]): row for row in _BUILTIN_FAMILIES}
    for row in _BUILTIN_FAMILIES:
        provider = source_catalog.get(row[0])
        models = provider.get("models", {}) if isinstance(provider, Mapping) else {}
        if not isinstance(models, Mapping):
            raise ProviderFamilyCatalogError("pinned provider models must be an object")
        builtin_rows.append((*row[:-1], len(models)))

    dynamic_rows: list[tuple[str, str, str, int]] = []
    for catalog_id, value in source_catalog.items():
        if not isinstance(value, Mapping) or value.get("id") != catalog_id:
            continue
        if catalog_id in builtin_by_id or not _FAMILY_ID.fullmatch(str(catalog_id)):
            continue
        display_name = str(value.get("name") or "").strip()
        if not display_name or len(display_name) > 256:
            continue
        npm = str(value.get("npm") or "@ai-sdk/openai-compatible")
        adapter_id = _MANAGED_ADAPTERS.get(npm)
        models = value.get("models")
        if adapter_id is None or not isinstance(models, Mapping):
            continue
        incompatible_model = False
        for model in models.values():
            if not isinstance(model, Mapping):
                incompatible_model = True
                break
            provider = model.get("provider")
            provider_npm = provider.get("npm") if isinstance(provider, Mapping) else None
            if str(provider_npm or npm) != npm:
                incompatible_model = True
                break
        if incompatible_model or not _public_https_url(value.get("api")):
            continue
        dynamic_rows.append(
            (str(catalog_id), display_name, adapter_id, len(models))
        )
    dynamic_rows.sort(key=lambda row: (row[1].casefold(), row[0]))
    return _payload(tuple(builtin_rows), tuple(dynamic_rows), identity=identity)


__all__ = [
    "ProviderFamilyCatalogError",
    "bundled_provider_family_catalog",
    "generate_provider_family_catalog",
]
