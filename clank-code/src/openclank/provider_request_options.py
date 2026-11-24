"""Typed, route-scoped request option descriptors for managed models.

Only choices explicitly declared by the current engine catalogue are exposed.
The helper is pure so HTTP/session owners can validate before persisting a
choice and again before applying it to an engine request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ProviderRequestOptionError(ValueError):
    """A selected model option is unsupported or based on a stale catalogue."""


def _effort_values(capabilities: Mapping[str, Any]) -> list[str]:
    raw = capabilities.get("reasoning_effort")
    if not isinstance(raw, (list, tuple)):
        return []
    values: list[str] = []
    seen: set[str] = set()
    for item in raw[:32]:
        if not isinstance(item, str) or not item or len(item) > 32:
            continue
        if item == "inherit" or any(ord(char) < 32 for char in item):
            continue
        if item not in seen:
            values.append(item)
            seen.add(item)
    return values


def project_request_options(
    capabilities: Mapping[str, Any] | None,
    *,
    catalog_revision: int,
    source: str,
) -> list[dict[str, Any]]:
    """Build public descriptors from explicit normalized route capabilities."""

    safe_revision = int(catalog_revision)
    if safe_revision < 1:
        return []
    safe_source = source if source in {"account-discovery", "engine-catalog"} else "engine-catalog"
    values = _effort_values(capabilities or {})
    if not values:
        return []
    descriptor = {
        "id": "reasoning_effort",
        "type": "enum",
        "label": "Thinking",
        "values": values,
        "default": "inherit",
        "scope": "model",
        "source": safe_source,
        "catalog_revision": safe_revision,
    }
    provider_default = (capabilities or {}).get("reasoning_default_effort")
    if isinstance(provider_default, str) and provider_default in values:
        descriptor["provider_default"] = provider_default
    return [descriptor]


def validate_request_option_selection(
    *,
    route_id: str,
    catalog_revision: int,
    descriptors: list[Mapping[str, Any]],
    selection: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Validate a selected value against one exact route and its revision.

    ``None`` or ``default``/``inherit`` means use the provider default and
    returns an empty override. Explicit strings such as ``none`` and ``0``
    remain values and are never collapsed into Default.
    """

    if selection is None:
        return {}
    if not isinstance(selection, Mapping):
        raise ProviderRequestOptionError("model option selection must be an object")
    expected_route = str(route_id or "").strip()
    selected_route = str(selection.get("route_id") or "").strip()
    selected_revision = selection.get("catalog_revision")
    expected_revision = catalog_revision
    if type(selected_revision) is not int or type(expected_revision) is not int:
        raise ProviderRequestOptionError("model option catalogue revision is invalid")
    if not expected_route or selected_route != expected_route or selected_revision != expected_revision:
        raise ProviderRequestOptionError("model options refer to a stale or different route")
    values = selection.get("options", {})
    if not isinstance(values, Mapping):
        raise ProviderRequestOptionError("model option values must be an object")
    if set(values) - {"reasoning_effort"}:
        raise ProviderRequestOptionError("model option is not supported")
    if "reasoning_effort" not in values:
        return {}
    selected = values["reasoning_effort"]
    if not isinstance(selected, str):
        raise ProviderRequestOptionError("reasoning effort must be a string")
    if selected in {"default", "inherit"}:
        return {}
    descriptor = next((item for item in descriptors if item.get("id") == "reasoning_effort"), None)
    if descriptor is None or type(descriptor.get("catalog_revision")) is not int or descriptor.get("catalog_revision") != expected_revision:
        raise ProviderRequestOptionError("reasoning options are unavailable for this route revision")
    allowed = descriptor.get("values")
    if not isinstance(allowed, list) or selected not in allowed:
        raise ProviderRequestOptionError("reasoning effort is not supported by this route")
    return {"reasoning_effort": selected}



def restore_child_receipt_options(
    receipts: list[Mapping[str, Any]], *, route_id: str, option_route_id: str,
    catalog_revision: int, descriptors: list[Mapping[str, Any]],
    provider_id: str, model_id: str, actor_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Use this child's last consumed receipt; absence means this route's Default."""
    previous = next((row for row in reversed(receipts) if row.get("state") == "consumed"), None)
    if previous is None:
        frozen = actor_snapshot
    else:
        frozen = previous.get("modelRequestOptions")
    if frozen is None or frozen == {"present": False}:
        return {}
    if not isinstance(frozen, Mapping):
        raise ProviderRequestOptionError("child model option receipt is unavailable; refresh required")
    if frozen.get("routeID") != route_id or frozen.get("model") != {"providerID": provider_id, "modelID": model_id}:
        raise ProviderRequestOptionError("child model option route changed; refresh required")
    return validate_request_option_selection(route_id=option_route_id, catalog_revision=catalog_revision,
        descriptors=descriptors, selection={"route_id": frozen.get("optionRouteID"),
            "catalog_revision": frozen.get("catalogRevision"), "options": frozen.get("options")})

def persist_request_option_selection(
    state: Mapping[str, Any] | None,
    *,
    route_id: str,
    catalog_revision: int,
    options: Mapping[str, str],
) -> dict[str, Any]:
    """Return a copy of session state with the route-bound option receipt.

    An empty override removes the stored value, which gives Default its
    omission semantics without disturbing unrelated Mimo session state.
    """

    result = dict(state or {})
    if not options:
        result.pop("model_request_options", None)
        return result
    if set(options) != {"reasoning_effort"}:
        raise ProviderRequestOptionError("model option is not supported")
    result["model_request_options"] = {
        "route_id": str(route_id),
        "catalog_revision": int(catalog_revision),
        "options": {"reasoning_effort": options["reasoning_effort"]},
    }
    return result


def persist_session_request_options(
    session_id: str,
    owner: str,
    *,
    route_id: str,
    option_route_id: str | None = None,
    catalog_revision: int,
    descriptors: list[Mapping[str, Any]],
    selection: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Persist one validated option against the session's currently selected route.

    The existing ``Session.mimo_state`` JSON is the durable home; no new column
    or schema migration is needed. The update takes a SQLite write lock and
    rereads the latest JSON so concurrent Mimo state fields are preserved.
    """

    from core.database import Session as DBSession, SessionLocal

    normalized_owner = str(owner or "").strip().lower()
    if not normalized_owner:
        raise ProviderRequestOptionError("model option persistence requires an owner")
    public_route_id = str(option_route_id or route_id)
    options = validate_request_option_selection(
        route_id=public_route_id,
        catalog_revision=catalog_revision,
        descriptors=descriptors,
        selection=selection,
    )
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        session = db.query(DBSession).filter(
            DBSession.id == str(session_id),
            DBSession.owner == normalized_owner,
        ).first()
        if session is None:
            raise ProviderRequestOptionError("chat session is not available for this owner")
        if str(session.provider_model_route_id or "") != str(route_id):
            raise ProviderRequestOptionError("model option selection does not match the chat route")
        state = dict(session.mimo_state or {})
        updated = persist_request_option_selection(
            state,
            route_id=public_route_id,
            catalog_revision=catalog_revision,
            options=options,
        )
        if updated != state:
            updated["revision"] = int(state.get("revision") or 0) + 1
            session.mimo_state = updated
            db.commit()
        else:
            db.rollback()
        return options
    finally:
        db.close()


def restore_session_request_options(
    session_id: str,
    owner: str,
    *,
    route_id: str,
    option_route_id: str | None = None,
    catalog_revision: int,
    descriptors: list[Mapping[str, Any]],
) -> dict[str, str]:
    """Reload an option only when its route and exact capability revision match."""

    from core.database import Session as DBSession, SessionLocal

    normalized_owner = str(owner or "").strip().lower()
    db = SessionLocal()
    try:
        session = db.query(DBSession).filter(
            DBSession.id == str(session_id),
            DBSession.owner == normalized_owner,
        ).first()
        if session is None:
            raise ProviderRequestOptionError("chat session is not available for this owner")
        if str(session.provider_model_route_id or "") != str(route_id):
            raise ProviderRequestOptionError("stored model options do not match the chat route")
        stored = (session.mimo_state or {}).get("model_request_options")
        if stored is None:
            return {}
        return validate_request_option_selection(
            route_id=str(option_route_id or route_id),
            catalog_revision=catalog_revision,
            descriptors=descriptors,
            selection=stored,
        )
    finally:
        db.close()
