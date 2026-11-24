"""Per-owner visibility (hide-list) for mimo provider-account models.

Provider-account connections (openai, xiaomi, ...) have no ModelEndpoint
row, so their per-model chat visibility lives in the mimo_model_prefs
table. Row present = hidden. Lives in core/ so both the model routes and
the dispatch-side resolver can share it without import cycles.
"""

from __future__ import annotations

import logging
from typing import Iterable

from core.database import MimoModelPref, SessionLocal

logger = logging.getLogger(__name__)


def _canonical_provider(provider_id: str | None) -> str:
    return str(provider_id or "").strip().lower()


def owner_hidden_mimo_model_ids(
    owner: str | None,
    provider_id: str | None = None,
) -> set[str]:
    """Model ids this owner hid on their provider accounts."""
    if not owner:
        return set()
    try:
        db = SessionLocal()
    except Exception:
        return set()
    try:
        query = db.query(MimoModelPref).filter(MimoModelPref.owner == owner)
        canonical = _canonical_provider(provider_id)
        if canonical:
            query = query.filter(MimoModelPref.provider_id == canonical)
        return {row.model_id for row in query.all()}
    except Exception as exc:
        logger.warning("Could not read mimo model visibility for owner %r: %s", owner, exc)
        return set()
    finally:
        db.close()


def set_owner_hidden_mimo_models(
    owner: str,
    provider_id: str,
    hidden: Iterable[str],
) -> int:
    """Replace one provider account's hide-list for an owner."""
    if not owner:
        return 0
    provider = _canonical_provider(provider_id)
    model_ids = sorted({
        str(item).strip()
        for item in hidden
        if item is not None and str(item).strip()
    })
    db = SessionLocal()
    try:
        db.query(MimoModelPref).filter(
            MimoModelPref.owner == owner,
            MimoModelPref.provider_id == provider,
        ).delete()
        for model_id in model_ids:
            db.add(MimoModelPref(owner=owner, provider_id=provider, model_id=model_id))
        db.commit()
    finally:
        db.close()
    return len(model_ids)
