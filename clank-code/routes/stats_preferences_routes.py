"""Validated owner preferences and permission-neutral alert candidates for Stats."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from core.database import SessionLocal
from services.stats.alerts import (
    StatsAlertError,
    acknowledge_alerts,
    evaluate_quota_alerts,
    normalize_preferences,
)
from services.stats.privacy import owner_scope
from services.stats.quota import quota_snapshot
from src.auth_helpers import effective_user, require_authenticated_request
import threading

_PREFERENCE_LOCK = threading.RLock()


_PREFERENCES_KEY = "stats_preferences"
_ALERT_STATE_KEY = "stats_alert_state"


def _identity(request: Request, *, test_state: bool = False) -> tuple[str, str | None]:
    require_authenticated_request(request)
    owner = str(effective_user(request) or "").strip().lower()
    if not owner:
        raise HTTPException(401, "trusted Stats owner scope is required")
    return owner, owner


def _default_pref_loader(user: str | None) -> dict:
    from routes.prefs_routes import _load_for_user
    return _load_for_user(user)


def _default_pref_saver(user: str | None, value: dict) -> None:
    from routes.prefs_routes import _save_for_user
    _save_for_user(user, value)


def setup_stats_preferences_routes(*, session_factory=SessionLocal,
                                   pref_loader=_default_pref_loader,
                                   pref_saver=_default_pref_saver,
                                   snapshot_loader=quota_snapshot,
                                   allow_test_owner_state: bool = False) -> APIRouter:
    router = APIRouter(prefix="/api/stats/v1", tags=["stats-preferences"])

    def load(owner: str, user: str | None) -> tuple[dict, dict]:
        stored = pref_loader(user)
        if not isinstance(stored, dict):
            stored = {}
        try:
            preferences = normalize_preferences(stored.get(_PREFERENCES_KEY))
        except StatsAlertError:
            preferences = normalize_preferences(None)
        stored[_PREFERENCES_KEY] = preferences
        state = stored.get(_ALERT_STATE_KEY)
        return stored, state if isinstance(state, dict) else {}

    @router.get("/preferences")
    async def get_preferences(request: Request):
        owner, user = _identity(request, test_state=allow_test_owner_state)
        stored, _state = load(owner, user)
        try:
            preferences = normalize_preferences(stored.get(_PREFERENCES_KEY))
        except StatsAlertError:
            preferences = normalize_preferences(None)
        return {"schema": "open-clank.stats.preferences.v1", "owner_scope": owner_scope(owner),
                "preferences": preferences}

    @router.put("/preferences")
    async def put_preferences(request: Request, payload: dict = Body(...)):
        owner, user = _identity(request, test_state=allow_test_owner_state)
        try:
            preferences = normalize_preferences(payload)
        except StatsAlertError as exc:
            raise HTTPException(422, str(exc)) from exc
        with _PREFERENCE_LOCK:
            stored = pref_loader(user)
            stored = dict(stored) if isinstance(stored, dict) else {}
            stored[_PREFERENCES_KEY] = preferences
            pref_saver(user, stored)
        return {"schema": "open-clank.stats.preferences.v1", "owner_scope": owner_scope(owner),
                "preferences": preferences}

    def current_candidates(owner: str, user: str | None):
        stored, state = load(owner, user)
        preferences = stored[_PREFERENCES_KEY]
        db = session_factory()
        try:
            snapshot = snapshot_loader(db, owner=owner)
        finally:
            db.close()
        observations = snapshot.get("observations", []) if isinstance(snapshot, dict) else []
        candidates, next_state = evaluate_quota_alerts(
            owner=owner, observations=observations, preferences=preferences, state=state,
        )
        stored[_PREFERENCES_KEY] = preferences
        stored[_ALERT_STATE_KEY] = next_state
        return stored, preferences, candidates, next_state

    @router.post("/alerts/evaluate")
    async def evaluate_alerts(request: Request):
        owner, user = _identity(request, test_state=allow_test_owner_state)
        try:
            with _PREFERENCE_LOCK:
                stored, _preferences, candidates, _state = current_candidates(owner, user)
                pref_saver(user, stored)
        except StatsAlertError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, "Stats quota alert source unavailable") from exc
        return {"schema": "open-clank.stats.alerts.v1", "owner_scope": owner_scope(owner),
                "delivery": "permission_required", "candidates": candidates}

    @router.post("/alerts/ack")
    async def acknowledge(request: Request, payload: dict = Body(...)):
        owner, user = _identity(request, test_state=allow_test_owner_state)
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        if (not isinstance(tokens, list) or len(tokens) > 100
                or any(not isinstance(token, str) or len(token) != 32 for token in tokens)):
            raise HTTPException(422, "tokens must contain at most 100 alert tokens")
        try:
            with _PREFERENCE_LOCK:
                stored, _preferences, candidates, state = current_candidates(owner, user)
                before = sum(len(row.get("delivered", [])) for row in state.get("windows", {}).values())
                stored[_ALERT_STATE_KEY] = acknowledge_alerts(state, candidates, tokens, owner=owner)
                after = sum(len(row.get("delivered", [])) for row in stored[_ALERT_STATE_KEY].get("windows", {}).values())
                pref_saver(user, stored)
        except Exception as exc:
            raise HTTPException(503, "Stats quota alert source unavailable") from exc
        return {"schema": "open-clank.stats.alerts.v1", "owner_scope": owner_scope(owner),
                "acknowledged": max(0, after - before)}

    return router


router = setup_stats_preferences_routes()
