"""Provider-neutral model catalog records and legacy wire projections."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional


def build_model_catalog(
    *,
    endpoint_id: str,
    endpoint_url: str,
    model_ids: Iterable[str],
    primary_ids: Iterable[str] = (),
    extra_ids: Iterable[str] = (),
    discovered: bool = True,
    entitled: Optional[bool] = None,
    stale: bool = False,
    hidden_ids: Iterable[str] = (),
    display_names: Optional[Mapping[str, str]] = None,
    capabilities: Optional[dict[str, Optional[bool]]] = None,
    compatibility: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Create the canonical catalog shape used by picker and Settings APIs.

    ``models``/``models_extra`` remain legacy projections for older clients;
    this list is the authority for state and capability metadata.
    """
    primary = set(primary_ids)
    extras = set(extra_ids)
    hidden = set(hidden_ids)
    displays = dict(display_names or {})
    caps = dict(capabilities or {})
    compat = dict(compatibility or {})
    result = []
    seen = set()
    for model_id in model_ids:
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        is_hidden = model_id in hidden
        is_compatible = bool(compat.get("compatible", not is_hidden))
        result.append({
            "endpoint_id": endpoint_id,
            "endpoint_url": endpoint_url,
            "model_id": model_id,
            "display_name": displays.get(model_id, model_id),
            "discovered": bool(discovered),
            "entitled": entitled,
            "compatible": is_compatible,
            "curated": model_id in primary or model_id not in extras,
            "hidden": is_hidden,
            "stale": bool(stale),
            "capabilities": dict(caps),
            "compatibility": dict(compat),
            "reason": (
                "hidden" if is_hidden
                else ("incompatible" if not is_compatible else ("stale" if stale else None))
            ),
        })
    return result
