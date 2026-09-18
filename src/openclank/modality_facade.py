"""Small application-facing helpers for the managed model-operation router."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from core.database import SessionLocal
from core.provider_models import (
    ProviderConnection,
    ProviderModelRoute,
    ProviderRouteBinding,
)

from src.openclank.operation_router import (
    ManagedOperationError,
    ManagedOperationUnavailable,
    ManagedOperationRequest,
    ManagedOperationResult,
    OperationArtifact,
    get_operation_router,
)


_TEXT_EXECUTION_MESSAGES: Mapping[str, str] = {
    "model_not_found": (
        "The selected model is not available in the managed runtime."
    ),
    "model_ineligible": (
        "The selected provider account cannot use this model."
    ),
    "provider_auth": (
        "The selected model's provider authentication needs attention."
    ),
    "provider_quota": (
        "The selected model's provider quota is currently exhausted."
    ),
    "provider_transient": (
        "The selected model's provider is temporarily unavailable."
    ),
    "provider_unknown": (
        "The selected model failed with an unknown provider outcome."
    ),
    "no_route_succeeded": "No configured model route succeeded.",
}


class ManagedTextCompletionError(ManagedOperationError):
    """Safe, typed failure returned by a terminal managed text operation."""

    def __init__(self, *, code: str, committed: bool) -> None:
        normalized = str(code or "managed_operation_failed").strip().lower()
        if not normalized.replace("_", "").isalnum():
            normalized = "managed_operation_failed"
        self.code = normalized
        self.committed = bool(committed)
        super().__init__(
            _TEXT_EXECUTION_MESSAGES.get(
                normalized,
                "The selected model could not complete the request.",
            )
        )


_TEXT_PURPOSE_SETTING_CHAINS: Mapping[str, tuple[str, ...]] = {
    "chat": ("default",),
    "utility": ("utility", "default"),
    "memory": ("memory", "utility", "default"),
    "research": ("research", "utility", "default"),
    "tasks": ("task", "utility", "default"),
}


def _configured_text_route(
    *,
    owner: str,
    purpose: str,
    operation: str = "chat.complete",
    provider_store: Any = None,
):
    """Resolve an explicit AI Defaults selection to one normalized route.

    Empty settings express inheritance, so Memory reads Utility on every call
    instead of copying Utility's current value.  Historical endpoint selectors
    that predate the normalized provider store are ignored here and continue to
    use their migrated durable purpose binding.  A stale normalized selector,
    especially a revoked share, fails closed rather than silently switching.
    """

    prefixes = _TEXT_PURPOSE_SETTING_CHAINS.get(str(purpose or "").strip().lower())
    if not prefixes:
        return None

    from src.openclank.chat_routing import (
        ChatRouteUnavailable,
        resolve_chat_route,
        share_id_from_endpoint,
    )
    from src.openclank.provider_store import ProviderNotFound, ProviderStore
    from src.settings import get_user_setting

    normalized_owner = str(owner or "").strip().lower() or "local-installation"
    store = provider_store or ProviderStore()
    for prefix in prefixes:
        endpoint_id = str(
            get_user_setting(f"{prefix}_endpoint_id", normalized_owner, "") or ""
        ).strip()
        model_id = str(
            get_user_setting(f"{prefix}_model", normalized_owner, "") or ""
        ).strip()
        if not endpoint_id and not model_id:
            continue
        normalized = share_id_from_endpoint(endpoint_id) is not None
        if endpoint_id and not normalized:
            try:
                store.get_connection(
                    owner=normalized_owner,
                    connection_id=endpoint_id,
                )
                normalized = True
            except ProviderNotFound:
                # Compatibility settings may still name a retired endpoint.
                # Their normalized route binding remains the execution source.
                continue

        if not endpoint_id or not model_id:
            if normalized:
                raise ManagedOperationUnavailable(
                    f"configured {prefix} model selection is incomplete"
                )
            # A partial historical selector cannot identify normalized
            # authority.  Let its migrated purpose binding remain canonical.
            continue

        if not normalized:
            continue
        try:
            if share_id_from_endpoint(endpoint_id) is not None:
                route = resolve_chat_route(
                    owner=normalized_owner,
                    endpoint_id=endpoint_id,
                    model_id=model_id,
                    provider_store=store,
                )
            else:
                try:
                    route = resolve_chat_route(
                        owner=normalized_owner,
                        endpoint_id=endpoint_id,
                        model_route_id=model_id,
                        provider_store=store,
                    )
                except ChatRouteUnavailable:
                    route = resolve_chat_route(
                        owner=normalized_owner,
                        endpoint_id=endpoint_id,
                        model_id=model_id,
                        provider_store=store,
                    )
        except ChatRouteUnavailable as exc:
            raise ManagedOperationUnavailable(
                f"configured {prefix} model route is unavailable"
            ) from exc
        if operation not in set(route.operations or ()):
            raise ManagedOperationUnavailable(
                f"configured {prefix} model does not support {operation}"
            )
        return route
    return None


def managed_route_preflight(
    *,
    owner: str,
    purpose: str,
    operation: str,
) -> dict[str, Any]:
    """Preflight the same effective route that execution will use."""

    router = get_operation_router()
    try:
        selected = _configured_text_route(
            owner=owner,
            purpose=purpose,
            operation=operation,
        )
    except ManagedOperationUnavailable as exc:
        result = router.route_preflight(
            owner=owner,
            purpose=purpose,
            operation=operation,
        )
        result["configured"] = False
        result["selection_error"] = str(exc)
        return result
    if selected is not None:
        return {
            "configured": True,
            "binding_revision": 0,
            "eligible_routes": [],
            "selected_model_route_id": selected.model_route_id,
            "selected_model_id": selected.provider_model_id,
            "selected_connection_id": selected.connection_id,
            "grant_id": selected.provider_grant_id,
        }
    return router.route_preflight(
        owner=owner,
        purpose=purpose,
        operation=operation,
    )


def _purpose(operation: str) -> str:
    if operation.startswith("image."):
        return "images"
    return {
        "chat.complete": "chat",
        "vision.describe": "vision",
        "audio.synthesize": "tts",
        "audio.transcribe": "stt",
        "embeddings.create": "embeddings",
    }[operation]


def managed_route_summary(
    *,
    owner: str,
    purpose: str,
    operation: str,
) -> Optional[dict[str, Any]]:
    """Return the first usable normalized route without exposing authority.

    This is intentionally a read-only UI/status projection.  Execution still
    resolves and revalidates the full ordered route set in
    :class:`ManagedOperationRouter` immediately before dispatch.
    """

    normalized_owner = str(owner or "").strip().lower() or "local-installation"
    with SessionLocal() as db:
        row = (
            db.query(ProviderRouteBinding, ProviderModelRoute, ProviderConnection)
            .join(
                ProviderModelRoute,
                ProviderModelRoute.id == ProviderRouteBinding.model_route_id,
            )
            .join(
                ProviderConnection,
                ProviderConnection.id == ProviderModelRoute.connection_id,
            )
            .filter(
                ProviderRouteBinding.owner == normalized_owner,
                ProviderRouteBinding.purpose == str(purpose).strip().lower(),
                ProviderRouteBinding.enabled.is_(True),
                ProviderModelRoute.owner == normalized_owner,
                ProviderModelRoute.enabled.is_(True),
                ProviderModelRoute.deleted_at.is_(None),
                ProviderConnection.owner == normalized_owner,
                ProviderConnection.enabled.is_(True),
                ProviderConnection.deleted_at.is_(None),
            )
            .order_by(ProviderRouteBinding.ordinal, ProviderRouteBinding.id)
            .first()
        )
        if row is None:
            return None
        binding, route, connection = row
        if operation not in set(route.operations or ()):
            return None
        return {
            "binding_id": binding.id,
            "model_route_id": route.id,
            "model_id": route.provider_model_id,
            "model_name": route.display_name,
            "connection_id": connection.id,
            "connection_label": connection.label,
            "family_id": connection.family_id,
            "adapter_id": connection.adapter_id,
            "connection_kind": connection.kind,
            "billing_lane": connection.billing_lane,
            "route_revision": int(route.revision),
            "catalog_revision": int(route.catalog_revision),
        }


async def execute_model_operation(
    *,
    owner: str,
    operation: str,
    input: Mapping[str, Any],
    artifacts: Sequence[OperationArtifact] = (),
    purpose: Optional[str] = None,
    model_route_id: Optional[str] = None,
    grant_id: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    options: Optional[Mapping[str, Any]] = None,
) -> ManagedOperationResult:
    normalized_owner = str(owner or "").strip().lower() or "local-installation"
    if operation in {"chat.complete", "vision.describe"} and not model_route_id and not grant_id:
        selected = _configured_text_route(
            owner=normalized_owner,
            purpose=purpose or _purpose(operation),
            operation=operation,
        )
        if selected is not None:
            model_route_id = selected.model_route_id
            grant_id = selected.provider_grant_id
    values: dict[str, Any] = {
        "owner": normalized_owner,
        "operation": operation,
        "purpose": purpose or _purpose(operation),
        "input": dict(input),
        "artifacts": tuple(artifacts),
        "model_route_id": model_route_id,
        "grant_id": grant_id,
        "options": dict(options or {}),
    }
    if root_operation_id:
        values["root_operation_id"] = root_operation_id
    if idempotency_key:
        values["idempotency_key"] = idempotency_key
    return await get_operation_router().execute(ManagedOperationRequest(**values))


async def complete_text(
    *,
    owner: str,
    messages: Sequence[Mapping[str, Any]],
    purpose: str = "utility",
    model_route_id: Optional[str] = None,
    grant_id: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
) -> str:
    """Run a tool-free text completion through the managed engine.

    Callers identify only the authorized owner, durable route purpose, and
    optional stable route/share identities.  Provider URLs, headers, and
    credentials deliberately cannot enter this boundary.
    """

    operation_input: dict[str, Any] = {
        "messages": [dict(message) for message in messages],
    }
    if temperature is not None:
        operation_input["temperature"] = float(temperature)
    if max_output_tokens is not None:
        tokens = int(max_output_tokens)
        if tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        operation_input["maxOutputTokens"] = tokens
    result = await execute_model_operation(
        owner=owner,
        operation="chat.complete",
        purpose=purpose,
        input=operation_input,
        model_route_id=model_route_id,
        grant_id=grant_id,
        root_operation_id=root_operation_id,
        idempotency_key=idempotency_key,
    )
    text = result.output.get("text")
    if result.state != "complete" or not isinstance(text, str):
        raise ManagedTextCompletionError(
            code=str(result.output.get("errorCode") or "managed_operation_failed"),
            committed=result.committed,
        )
    return text


async def execute_with_bytes(
    *,
    owner: str,
    operation: str,
    input: Mapping[str, Any],
    byte_inputs: Sequence[tuple[str, bytes, str]] = (),
    **kwargs: Any,
) -> ManagedOperationResult:
    router = get_operation_router()
    normalized_owner = str(owner or "").strip().lower() or "local-installation"
    artifacts = tuple(
        router.stage_artifact(
            owner=normalized_owner,
            name=name,
            data=data,
            media_type=media_type,
        )
        for name, data, media_type in byte_inputs
    )
    return await execute_model_operation(
        owner=normalized_owner,
        operation=operation,
        input=input,
        artifacts=artifacts,
        **kwargs,
    )


def read_first_output(
    *,
    owner: str,
    result: ManagedOperationResult,
    acknowledge: bool = True,
) -> tuple[bytes, str]:
    if result.state != "complete" or not result.artifacts:
        raise RuntimeError("managed model operation returned no artifact")
    artifact = result.artifacts[0]
    return (
        get_operation_router().read_artifact(
            owner=str(owner or "").strip().lower() or "local-installation",
            artifact=artifact,
            acknowledge=acknowledge,
        ),
        artifact.media_type,
    )


async def describe_image(
    *,
    owner: str,
    image: bytes,
    media_type: str,
    prompt: str = "Describe this image in detail",
    model_route_id: Optional[str] = None,
    grant_id: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> ManagedOperationResult:
    return await execute_with_bytes(
        owner=owner,
        operation="vision.describe",
        input={"prompt": prompt},
        byte_inputs=(("image", image, media_type),),
        model_route_id=model_route_id,
        grant_id=grant_id,
        root_operation_id=root_operation_id,
        idempotency_key=idempotency_key,
    )


async def describe_memory_photo(
    *,
    owner: str,
    image: bytes,
    media_type: str,
    prompt: str = "Extract useful memory context from this image",
    model_route_id: Optional[str] = None,
    grant_id: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> ManagedOperationResult:
    """Describe a photo through the effective Memory route.

    This intentionally uses ``vision.describe`` as the operation while
    retaining ``purpose='memory'``. It cannot silently select the separate
    Vision binding.
    """
    return await execute_with_bytes(
        owner=owner,
        operation="vision.describe",
        purpose="memory",
        input={"prompt": prompt},
        byte_inputs=(("memory-photo", image, media_type),),
        model_route_id=model_route_id,
        grant_id=grant_id,
        root_operation_id=root_operation_id,
        idempotency_key=idempotency_key,
    )


async def describe_image_path(
    *,
    owner: str,
    image_path: str | Path,
    prompt: str = "Describe this image in detail",
    model_route_id: Optional[str] = None,
    grant_id: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> ManagedOperationResult:
    path = Path(image_path)
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return await describe_image(
        owner=owner,
        image=path.read_bytes(),
        media_type=media_type,
        prompt=prompt,
        model_route_id=model_route_id,
        grant_id=grant_id,
        root_operation_id=root_operation_id,
        idempotency_key=idempotency_key,
    )


async def generate_image(
    *,
    owner: str,
    prompt: str,
    size: str = "1024x1024",
    quality: str = "medium",
    model_route_id: Optional[str] = None,
    grant_id: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    options: Optional[Mapping[str, Any]] = None,
) -> tuple[bytes, str, ManagedOperationResult]:
    """Generate one image through the managed engine and artifact spool."""

    result = await execute_model_operation(
        owner=owner,
        operation="image.generate",
        input={
            "prompt": prompt,
            "size": size,
            "quality": quality,
        },
        model_route_id=model_route_id,
        grant_id=grant_id,
        root_operation_id=root_operation_id,
        idempotency_key=idempotency_key,
        options=options,
    )
    data, media_type = read_first_output(owner=owner, result=result)
    return data, media_type, result


async def transform_image(
    *,
    owner: str,
    operation: str,
    image: bytes,
    media_type: str,
    input: Optional[Mapping[str, Any]] = None,
    mask: Optional[bytes] = None,
    mask_media_type: str = "image/png",
    model_route_id: Optional[str] = None,
    grant_id: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    options: Optional[Mapping[str, Any]] = None,
) -> tuple[bytes, str, ManagedOperationResult]:
    """Run one image transform without exposing a provider URL or credential.

    Artifact order is stable (source image first, optional mask second), which
    is also the fixed contract used by registered local executor recipes.
    """

    if operation not in {
        "image.edit",
        "image.inpaint",
        "image.img2img",
        "image.upscale",
        "image.denoise",
        "image.segment",
        "image.remove_background",
        "image.restore_face",
    }:
        raise ValueError("unsupported managed image transform")
    byte_inputs: list[tuple[str, bytes, str]] = [
        ("image", image, media_type),
    ]
    if mask is not None:
        byte_inputs.append(("mask", mask, mask_media_type))
    result = await execute_with_bytes(
        owner=owner,
        operation=operation,
        input=dict(input or {}),
        byte_inputs=tuple(byte_inputs),
        model_route_id=model_route_id,
        grant_id=grant_id,
        root_operation_id=root_operation_id,
        idempotency_key=idempotency_key,
        options=options,
    )
    data, output_media_type = read_first_output(owner=owner, result=result)
    return data, output_media_type, result


async def synthesize_audio(
    *,
    owner: str,
    text: str,
    voice: str,
    speed: float,
    response_format: str = "mp3",
    model_route_id: Optional[str] = None,
) -> tuple[bytes, str, ManagedOperationResult]:
    result = await execute_model_operation(
        owner=owner,
        operation="audio.synthesize",
        input={
            "text": text,
            "voice": voice,
            "speed": speed,
            "responseFormat": response_format,
        },
        model_route_id=model_route_id,
    )
    data, media_type = read_first_output(owner=owner, result=result)
    return data, media_type, result


async def transcribe_audio(
    *,
    owner: str,
    audio: bytes,
    media_type: str,
    language: str = "",
    model_route_id: Optional[str] = None,
) -> ManagedOperationResult:
    return await execute_with_bytes(
        owner=owner,
        operation="audio.transcribe",
        input={"language": language},
        byte_inputs=(("audio", audio, media_type),),
        model_route_id=model_route_id,
    )


async def create_embeddings(
    *,
    owner: str,
    texts: Sequence[str],
    content_purpose: str = "document",
    model_route_id: Optional[str] = None,
    root_operation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> ManagedOperationResult:
    return await execute_model_operation(
        owner=owner,
        operation="embeddings.create",
        input={"texts": list(texts), "purpose": content_purpose},
        model_route_id=model_route_id,
        root_operation_id=root_operation_id,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "complete_text",
    "create_embeddings",
    "describe_image",
    "describe_image_path",
    "execute_model_operation",
    "execute_with_bytes",
    "generate_image",
    "managed_route_summary",
    "read_first_output",
    "synthesize_audio",
    "transform_image",
    "transcribe_audio",
]
