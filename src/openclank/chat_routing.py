"""Normalized chat-route resolution for the Open Clank control plane.

Public chat clients retain the historical ``endpoint_id``/``model`` response
shape, but those fields are now a nonsecret display selector only.  Execution
is authorized by a stable :class:`ProviderModelRoute` ID and, for received
shares, an active :class:`ProviderShareGrant` ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from core.database import SessionLocal
from core.provider_models import ProviderModelRoute
from src.openclank.provider_store import (
    ProviderNotFound,
    ProviderStore,
    ProviderStoreError,
    ShareDenied,
)
from src.secret_storage import keyed_digest


LOCAL_INSTALLATION_OWNER = "local-installation"
MANAGED_ENGINE_PUBLIC_URL = "openclank://engine"
SHARED_ENDPOINT_PREFIX = "share:"
CHAT_OPERATIONS = frozenset({"chat.stream", "chat.complete"})


class ChatRouteUnavailable(ValueError):
    """The requested normalized route is not live for this caller."""


@dataclass(frozen=True)
class NormalizedChatRoute:
    """One authorization-checked route suitable for chat dispatch or display."""

    model_route_id: str
    connection_id: str
    provider_model_id: str
    display_name: str
    connection_label: str
    family_id: str
    adapter_id: str
    connection_kind: str
    billing_lane: str
    operations: tuple[str, ...]
    capabilities: dict[str, Any]
    public_endpoint_id: str
    provider_grant_id: str | None = None
    share_label: str | None = None
    disclosed_owner: str | None = None

    @property
    def runtime_model(self) -> str:
        return f"{self.connection_id}/{self.provider_model_id}"

    @property
    def shared(self) -> bool:
        return self.provider_grant_id is not None


def normalized_provider_owner(owner: str | None) -> str:
    return str(owner or "").strip().lower() or LOCAL_INSTALLATION_OWNER


def shared_endpoint_id(grant_id: str) -> str:
    value = str(grant_id or "").strip()
    if not value:
        raise ChatRouteUnavailable("Shared provider route is missing its grant")
    return f"{SHARED_ENDPOINT_PREFIX}{value}"


def shared_provider_group_id(
    *,
    recipient: str,
    owner: str,
    connection_id: str,
) -> str:
    """Return a stable recipient-scoped opaque source-provider grouping ID."""

    normalized_recipient = normalized_provider_owner(recipient)
    normalized_owner = normalized_provider_owner(owner)
    normalized_connection = _validate_connection_identity(connection_id)
    digest = keyed_digest(
        f"{normalized_owner}\0{normalized_connection}",
        context=f"provider-share-group:{normalized_recipient}",
    )
    return f"shared_provider_{digest[:24]}"


def share_id_from_endpoint(endpoint_id: str | None) -> str | None:
    value = str(endpoint_id or "").strip()
    if not value.startswith(SHARED_ENDPOINT_PREFIX):
        return None
    grant_id = value[len(SHARED_ENDPOINT_PREFIX):].strip()
    return grant_id or None


def _route_is_chat_capable(route: Any) -> bool:
    return bool(
        route
        and route.enabled
        and route.deleted_at is None
        and str(route.visibility or "visible") == "visible"
        and CHAT_OPERATIONS.intersection(str(value) for value in (route.operations or ()))
    )


def _validate_connection_identity(connection_id: Any) -> str:
    value = str(connection_id or "").strip()
    if (
        not value
        or "/" in value
        or value.startswith(SHARED_ENDPOINT_PREFIX)
    ):
        raise ChatRouteUnavailable(
            "Selected provider connection has an invalid managed identity"
        )
    return value


def _detached_route_owner(model_route_id: str) -> str | None:
    """Read only the owning principal needed to authorize a stored route ID."""

    db = SessionLocal()
    try:
        row = db.query(ProviderModelRoute.owner).filter(
            ProviderModelRoute.id == str(model_route_id or "").strip(),
        ).first()
        return str(row.owner) if row is not None else None
    finally:
        db.close()


def _build_owned(
    store: ProviderStore,
    *,
    owner: str,
    connection_id: str,
    route: Any,
) -> NormalizedChatRoute:
    connection = store.get_connection(owner=owner, connection_id=connection_id)
    if not connection.enabled or connection.deleted_at is not None:
        raise ChatRouteUnavailable("Selected provider connection is unavailable")
    if route.connection_id != connection.id or not _route_is_chat_capable(route):
        raise ChatRouteUnavailable("Selected model is not available for chat")
    if not store.route_has_permitted_account(
        owner=owner,
        model_route_id=route.id,
    ):
        raise ChatRouteUnavailable("Selected model is not entitled for this account")
    normalized_connection_id = _validate_connection_identity(connection.id)
    return NormalizedChatRoute(
        model_route_id=route.id,
        connection_id=normalized_connection_id,
        provider_model_id=route.provider_model_id,
        display_name=route.display_name,
        connection_label=connection.label,
        family_id=connection.family_id,
        adapter_id=connection.adapter_id,
        connection_kind=connection.kind,
        billing_lane=connection.billing_lane,
        operations=tuple(str(value) for value in (route.operations or ())),
        capabilities=dict(route.capabilities or {}),
        public_endpoint_id=normalized_connection_id,
    )


def _build_shared(
    store: ProviderStore,
    *,
    recipient: str,
    grant: Any,
    route: Any,
) -> NormalizedChatRoute:
    scope = store.resolve_share_scope(
        recipient=recipient,
        grant_id=grant.id,
    )
    if route.id not in scope.model_route_ids or not _route_is_chat_capable(route):
        raise ChatRouteUnavailable("Shared model is not available for chat")
    if not store.route_has_permitted_account(
        owner=scope.owner,
        model_route_id=route.id,
        allowed_account_ids=scope.account_ids,
    ):
        raise ChatRouteUnavailable("Shared model is not entitled for this account")
    connection = store.get_connection(
        owner=scope.owner,
        connection_id=scope.connection_id,
    )
    normalized_connection_id = _validate_connection_identity(connection.id)
    return NormalizedChatRoute(
        model_route_id=route.id,
        connection_id=normalized_connection_id,
        provider_model_id=route.provider_model_id,
        display_name=route.display_name,
        connection_label=grant.label,
        family_id=connection.family_id,
        adapter_id=connection.adapter_id,
        connection_kind=connection.kind,
        billing_lane=scope.billing_lane,
        operations=tuple(str(value) for value in (route.operations or ())),
        capabilities=dict(route.capabilities or {}),
        public_endpoint_id=shared_endpoint_id(grant.id),
        provider_grant_id=grant.id,
        share_label=grant.label,
        # Sharing is never anonymous: the normalized username is safe display
        # attribution while source provider/account/model IDs stay private.
        disclosed_owner=scope.owner,
    )


def _route_for_source(
    store: ProviderStore,
    *,
    source_owner: str,
    model_route_id: str,
    model_id: str | None,
) -> Any:
    route = store.get_model_route(
        owner=source_owner,
        model_route_id=model_route_id,
    )
    requested_model = str(model_id or "").strip()
    if requested_model and route.provider_model_id != requested_model:
        raise ChatRouteUnavailable("Selected model does not match its stable route")
    return route


def _matching_grants(
    store: ProviderStore,
    *,
    recipient: str,
    model_route_id: str,
) -> list[Any]:
    matches: list[Any] = []
    for grant in store.list_share_grants(recipient=recipient):
        try:
            scope = store.resolve_share_scope(
                recipient=recipient,
                grant_id=grant.id,
            )
        except ShareDenied:
            continue
        if model_route_id in scope.model_route_ids:
            matches.append(grant)
    return matches


def resolve_chat_route(
    *,
    owner: str | None,
    endpoint_id: str | None = None,
    model_id: str | None = None,
    model_route_id: str | None = None,
    provider_store: ProviderStore | None = None,
) -> NormalizedChatRoute:
    """Resolve one exact own/shared route without consulting legacy stores."""

    recipient = normalized_provider_owner(owner)
    endpoint = str(endpoint_id or "").strip()
    route_id = str(model_route_id or "").strip()
    model = str(model_id or "").strip()
    store = provider_store or ProviderStore()
    try:
        grant_id = share_id_from_endpoint(endpoint)
        if grant_id is not None:
            grant = store.get_share_grant(grant_id=grant_id, recipient=recipient)
            scope = store.resolve_share_scope(
                recipient=recipient,
                grant_id=grant.id,
            )
            candidates = []
            for candidate_id in scope.model_route_ids:
                route = store.get_model_route(
                    owner=scope.owner,
                    model_route_id=candidate_id,
                )
                if route_id and route.id != route_id:
                    continue
                if model and route.provider_model_id != model:
                    continue
                if _route_is_chat_capable(route):
                    candidates.append(route)
            if len(candidates) != 1:
                raise ChatRouteUnavailable(
                    "Shared provider selection does not identify one live chat model"
                )
            return _build_shared(
                store,
                recipient=recipient,
                grant=grant,
                route=candidates[0],
            )

        if route_id:
            source_owner = _detached_route_owner(route_id)
            if source_owner is None:
                raise ChatRouteUnavailable("Selected model route no longer exists")
            route = _route_for_source(
                store,
                source_owner=source_owner,
                model_route_id=route_id,
                model_id=model,
            )
            if source_owner == recipient:
                if endpoint and endpoint != route.connection_id:
                    raise ChatRouteUnavailable(
                        "Selected connection does not match its stable model route"
                    )
                return _build_owned(
                    store,
                    owner=recipient,
                    connection_id=route.connection_id,
                    route=route,
                )
            grants = _matching_grants(
                store,
                recipient=recipient,
                model_route_id=route.id,
            )
            if len(grants) != 1:
                raise ChatRouteUnavailable(
                    "Stored shared route no longer identifies one active grant"
                )
            return _build_shared(
                store,
                recipient=recipient,
                grant=grants[0],
                route=route,
            )

        if not endpoint or not model:
            raise ChatRouteUnavailable("Choose a provider connection and model")
        routes = [
            route
            for route in store.list_model_routes(
                owner=recipient,
                connection_id=endpoint,
            )
            if route.provider_model_id == model and _route_is_chat_capable(route)
        ]
        if len(routes) != 1:
            raise ChatRouteUnavailable(
                "Provider selection does not identify one live chat model"
            )
        return _build_owned(
            store,
            owner=recipient,
            connection_id=endpoint,
            route=routes[0],
        )
    except ChatRouteUnavailable:
        raise
    except (ProviderNotFound, ProviderStoreError, ShareDenied) as exc:
        raise ChatRouteUnavailable("Selected provider route is unavailable") from exc


def list_chat_routes(
    owner: str | None,
    *,
    provider_store: ProviderStore | None = None,
) -> tuple[list[NormalizedChatRoute], list[NormalizedChatRoute]]:
    """Return live own routes and active received routes, both secret-free."""

    recipient = normalized_provider_owner(owner)
    store = provider_store or ProviderStore()
    own: list[NormalizedChatRoute] = []
    for connection in store.list_connections(owner=recipient):
        if not connection.enabled or connection.deleted_at is not None:
            continue
        for route in store.list_model_routes(
            owner=recipient,
            connection_id=connection.id,
        ):
            if not _route_is_chat_capable(route):
                continue
            if not store.route_has_permitted_account(
                owner=recipient,
                model_route_id=route.id,
            ):
                continue
            own.append(
                _build_owned(
                    store,
                    owner=recipient,
                    connection_id=connection.id,
                    route=route,
                )
            )

    shared: list[NormalizedChatRoute] = []
    for grant in store.list_share_grants(recipient=recipient):
        try:
            scope = store.resolve_share_scope(
                recipient=recipient,
                grant_id=grant.id,
            )
            for route_id in scope.model_route_ids:
                route = store.get_model_route(
                    owner=scope.owner,
                    model_route_id=route_id,
                )
                if _route_is_chat_capable(route):
                    if not store.route_has_permitted_account(
                        owner=scope.owner,
                        model_route_id=route.id,
                        allowed_account_ids=scope.account_ids,
                    ):
                        continue
                    shared.append(
                        _build_shared(
                            store,
                            recipient=recipient,
                            grant=grant,
                            route=route,
                        )
                    )
        except (ProviderStoreError, ShareDenied, ChatRouteUnavailable):
            # Revoked, stale, or disabled grants never enter the public
            # catalogue. One bad grant must not hide the caller's own otherwise
            # valid routes.
            continue
    return own, shared


def resolve_chat_model_spec(
    *,
    owner: str | None,
    model_spec: str | None,
    provider_store: ProviderStore | None = None,
) -> NormalizedChatRoute:
    """Resolve a user-facing model selector against the normalized catalog.

    Stable model-route IDs are preferred.  Provider model/display names remain
    a compatibility convenience only when they identify exactly one visible
    route.  ``auto`` follows the durable chat binding before catalog order.
    """

    recipient = normalized_provider_owner(owner)
    store = provider_store or ProviderStore()
    own, shared = list_chat_routes(recipient, provider_store=store)
    routes = [*own, *shared]
    if not routes:
        raise ChatRouteUnavailable("No normalized chat model is available")

    raw = str(model_spec or "").strip()
    if not raw or raw.casefold() == "auto":
        by_id = {route.model_route_id: route for route in routes}
        for binding in store.list_route_bindings(owner=recipient):
            if (
                binding.purpose == "chat"
                and binding.enabled
                and binding.model_route_id in by_id
            ):
                return by_id[binding.model_route_id]
        return routes[0]

    model_part, separator, endpoint_part = raw.rpartition("@")
    requested_model = (model_part if separator else raw).strip().casefold()
    requested_endpoint = endpoint_part.strip().casefold() if separator else ""

    def endpoint_matches(route: NormalizedChatRoute) -> bool:
        if not requested_endpoint:
            return True
        visible_ids = {
            route.public_endpoint_id.casefold(),
            route.connection_label.casefold(),
        }
        if not route.shared:
            visible_ids.add(route.connection_id.casefold())
        return requested_endpoint in visible_ids

    exact = [
        route
        for route in routes
        if endpoint_matches(route)
        and requested_model
        in {
            route.model_route_id.casefold(),
            route.provider_model_id.casefold(),
            route.display_name.casefold(),
        }
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ChatRouteUnavailable(
            "Model selector is ambiguous; use its stable model-route ID"
        )

    partial = [
        route
        for route in routes
        if endpoint_matches(route)
        and (
            requested_model in route.provider_model_id.casefold()
            or requested_model in route.display_name.casefold()
        )
    ]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise ChatRouteUnavailable(
            "Model selector is ambiguous; use its stable model-route ID"
        )
    raise ChatRouteUnavailable("Selected normalized chat model is unavailable")


def group_chat_routes(
    routes: Iterable[NormalizedChatRoute],
) -> dict[str, list[NormalizedChatRoute]]:
    result: dict[str, list[NormalizedChatRoute]] = {}
    for route in routes:
        result.setdefault(route.public_endpoint_id, []).append(route)
    for values in result.values():
        values.sort(key=lambda value: (value.display_name.casefold(), value.model_route_id))
    return result


__all__ = [
    "CHAT_OPERATIONS",
    "ChatRouteUnavailable",
    "MANAGED_ENGINE_PUBLIC_URL",
    "NormalizedChatRoute",
    "group_chat_routes",
    "list_chat_routes",
    "normalized_provider_owner",
    "resolve_chat_route",
    "resolve_chat_model_spec",
    "share_id_from_endpoint",
    "shared_endpoint_id",
    "shared_provider_group_id",
]
