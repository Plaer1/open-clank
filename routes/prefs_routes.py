"""User preferences API — per-user key/value store backed by a JSON file."""
import json
import os
from typing import Iterable, Optional
from fastapi import APIRouter, HTTPException, Request
from src.auth_helpers import get_current_user
from src.constants import USER_PREFS_FILE

PREFS_FILE = USER_PREFS_FILE
_MEMORY_MODES = frozenset({"off", "automatic", "manual"})
_PERMISSION_MODES = frozenset({"manual", "yolo", "auto"})
_TRUST_PREF_MASTER = "memory_trust_auto"
_TRUST_PREF_KINDS = "memory_trust_auto_kinds"
_TRUST_KINDS = frozenset({"instruction", "persona", "fact", "episodic", "fabric", "wiki"})
_MOBILE_CONTROL_SIDE_PREF = "mobile_control_side"
_MOBILE_CONTROL_SIDES = frozenset({"left", "right", "system"})


def _load():
    """Load the raw prefs file (internal use only)."""
    try:
        with open(PREFS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(prefs):
    os.makedirs(os.path.dirname(PREFS_FILE) or ".", exist_ok=True)
    tmp = f"{PREFS_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PREFS_FILE)


def _load_for_user(user: Optional[str] = None) -> dict:
    """Load preferences for a specific user."""
    all_prefs = _load()
    if "_users" in all_prefs:
        if user is None:
            # Auth disabled — return first user's prefs for backward compat
            users = all_prefs["_users"]
            return dict(next(iter(users.values()), {}))
        return dict(all_prefs["_users"].get(user, {}))
    # Legacy flat format — return as-is
    return dict(all_prefs)


def _save_for_user(user: Optional[str], prefs: dict):
    """Save preferences for a specific user."""
    all_prefs = _load()
    if user is None:
        # Auth disabled. If the store is already multi-user (e.g. auth was
        # turned off on a deployment that previously ran multi-user), writing
        # `prefs` flat would overwrite the whole `_users` map and destroy every
        # other user's preferences. Instead write back into the same (first)
        # slot _load_for_user(None) reads from, preserving the others.
        if "_users" in all_prefs:
            users = all_prefs["_users"]
            first_key = next(iter(users), None)
            if first_key is not None:
                users[first_key] = prefs
                _save(all_prefs)
                return
        _save(prefs)
        return
    if "_users" not in all_prefs:
        all_prefs = {"_users": {}}
    all_prefs["_users"][user] = prefs
    _save(all_prefs)


def _mobile_control_side(value) -> str:
    """Normalize the mobile layout preference without mutating stored prefs."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _MOBILE_CONTROL_SIDES:
            return normalized
    return "system"


def backfill_memory_modes(existing_users: Iterable[str] = ()) -> bool:
    """Freeze pre-v2 account behavior before the missing default becomes manual."""
    all_prefs = _load()
    users = [str(user or "").strip().lower() for user in existing_users]
    users = list(dict.fromkeys(user for user in users if user))
    changed = False

    def freeze(prefs: dict) -> None:
        nonlocal changed
        raw = str(prefs.get("memory_mode") or "").strip().lower()
        if raw in _MEMORY_MODES:
            if prefs.get("memory_mode") != raw:
                prefs["memory_mode"] = raw
                changed = True
            return
        prefs["memory_mode"] = "off" if prefs.get("auto_memory") is False else "automatic"
        changed = True

    scoped = all_prefs.get("_users")
    if isinstance(scoped, dict):
        targets = users or [str(user) for user in scoped]
        for user in targets:
            prefs = scoped.get(user)
            if not isinstance(prefs, dict):
                prefs = {}
                scoped[user] = prefs
                changed = True
            freeze(prefs)
    elif users:
        # The historical flat store belonged to the sole configured account.
        # Database startup already performs this conversion; retain the same
        # deterministic rule here for startup orders that reach prefs first.
        if len(users) == 1:
            prefs = dict(all_prefs)
            freeze(prefs)
            all_prefs = {"_users": {users[0]: prefs}}
            changed = True
        else:
            all_prefs = {"_users": {}}
            for user in users:
                prefs = {}
                freeze(prefs)
                all_prefs["_users"][user] = prefs
            changed = True
    elif all_prefs:
        freeze(all_prefs)

    if changed:
        _save(all_prefs)
    return changed


def setup_prefs_routes():
    router = APIRouter(prefix="/api/prefs", tags=["preferences"])

    @router.get("")
    async def get_all_prefs(request: Request):
        user = get_current_user(request)
        prefs = _load_for_user(user)
        if _MOBILE_CONTROL_SIDE_PREF in prefs:
            prefs[_MOBILE_CONTROL_SIDE_PREF] = _mobile_control_side(
                prefs[_MOBILE_CONTROL_SIDE_PREF]
            )
        return prefs

    @router.get("/{key}")
    async def get_pref(request: Request, key: str):
        user = get_current_user(request)
        prefs = _load_for_user(user)
        value = prefs.get(key)
        if key == "permission_mode":
            value = value if value in _PERMISSION_MODES else "manual"
        elif key == _MOBILE_CONTROL_SIDE_PREF:
            value = _mobile_control_side(value)
        return {"key": key, "value": value}

    @router.put("/{key}")
    async def set_pref(request: Request, key: str, body: dict):
        user = get_current_user(request)
        prefs = _load_for_user(user)
        value = body.get("value") if isinstance(body, dict) else None
        if key == "permission_mode":
            if not isinstance(value, str) or value.strip().lower() not in _PERMISSION_MODES:
                raise HTTPException(status_code=422, detail="permission_mode must be manual, yolo, or auto")
            value = value.strip().lower()
        elif key == _TRUST_PREF_MASTER:
            if not isinstance(value, bool):
                raise HTTPException(status_code=422, detail="memory_trust_auto must be a boolean")
        elif key == _TRUST_PREF_KINDS:
            if not isinstance(value, dict) or any(
                kind not in _TRUST_KINDS or not isinstance(enabled, bool)
                for kind, enabled in value.items()
            ):
                raise HTTPException(
                    status_code=422,
                    detail="memory_trust_auto_kinds must map known kinds to booleans",
                )
            value = {kind: value[kind] for kind in sorted(value)}
        elif key == _MOBILE_CONTROL_SIDE_PREF:
            if not isinstance(value, str) or value.strip().lower() not in _MOBILE_CONTROL_SIDES:
                raise HTTPException(
                    status_code=422,
                    detail="mobile_control_side must be left, right, or system",
                )
            value = value.strip().lower()
        prefs[key] = value
        _save_for_user(user, prefs)
        return {"key": key, "value": prefs[key]}

    return router
