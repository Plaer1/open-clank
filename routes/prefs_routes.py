"""User preferences API — per-user key/value store backed by a JSON file."""
import json
import os
import threading
import tempfile

PREFERENCE_LOCK = threading.RLock()
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
    fd, tmp = tempfile.mkstemp(prefix=".prefs-", dir=os.path.dirname(PREFS_FILE) or ".")
    os.close(fd)
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
    if user is not None and all_prefs:
        raise ValueError("Legacy preferences require .clanker/tools/migrations/python/secondary.py prefs")
    return dict(all_prefs)


def _save_for_user_unlocked(user: Optional[str], prefs: dict):
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
        if all_prefs:
            raise ValueError("Legacy preferences require .clanker/tools/migrations/python/secondary.py prefs")
        all_prefs = {"_users": {}}
    all_prefs["_users"][user] = prefs
    _save(all_prefs)


def _save_for_user(user: Optional[str], prefs: dict, *, allow_logging=False):
    with PREFERENCE_LOCK:
        updated = dict(prefs)
        if not allow_logging:
            current = _load_for_user(user)
            if "logging_preferences" in current:
                updated["logging_preferences"] = current["logging_preferences"]
            else:
                updated.pop("logging_preferences", None)
        return _save_for_user_unlocked(user, updated)


def _update_for_user(user, patch):
    with PREFERENCE_LOCK:
        current = _load_for_user(user)
        current.update(patch)
        _save_for_user(user, current)
        return current


def _mobile_control_side(value) -> str:
    """Normalize the mobile layout preference without mutating stored prefs."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _MOBILE_CONTROL_SIDES:
            return normalized
    return "system"




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
        if key == "logging_preferences":
            raise HTTPException(422, "Use the validated Logging policy endpoint")
        prefs = _update_for_user(user, {key: value})
        return {"key": key, "value": prefs[key]}

    return router
