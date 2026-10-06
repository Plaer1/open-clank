"""Account-owned logging preferences and immutable managed-operation pins."""
from __future__ import annotations

import copy
import threading
import time
from typing import Mapping

from routes.prefs_routes import PREFERENCE_LOCK
SCHEMA = "open-clank.logging.policy.v1"
BOOLS = {"advanced_enabled", "request_body_enabled", "response_body_enabled", "binary_body_enabled", "live_refresh"}


class LoggingPolicyError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def defaults():
    return {"schema": SCHEMA, "revision": 1, "advanced_enabled": False,
            "request_body_enabled": True, "response_body_enabled": True, "binary_body_enabled": False,
            "body_retention": {"mode": "keep_until_deleted"}, "metadata_retention": {"mode": "keep_until_deleted"},
            "live_refresh": True, "timezone": "local", "initial_period": "30d", "saved_filters": {}}


def normalize(value):
    if value is None:
        return defaults()
    if isinstance(value, Mapping) and "ordinary_enabled" in value:
        raise LoggingPolicyError("legacy_policy_conversion_required", "Run the explicit logging_policy_detail.py preference conversion before using this saved policy")
    if not isinstance(value, Mapping) or set(value) - set(defaults()):
        raise LoggingPolicyError("invalid_policy", "Unknown logging preference")
    result = {**defaults(), **copy.deepcopy(dict(value))}
    if result["schema"] != SCHEMA or isinstance(result["revision"], bool) or not isinstance(result["revision"], int) or result["revision"] < 1:
        raise LoggingPolicyError("invalid_policy", "Invalid logging policy revision")
    for key in BOOLS:
        if not isinstance(result[key], bool):
            raise LoggingPolicyError("invalid_policy", f"{key} must be boolean")
    for key in ("body_retention", "metadata_retention"):
        retention = result[key]
        if not isinstance(retention, dict) or retention.get("mode") not in {"keep_until_deleted", "age", "size"}:
            raise LoggingPolicyError("invalid_policy", "Invalid retention mode")
        mode = retention["mode"]
        field = {"age": "days", "size": "max_bytes"}.get(mode)
        if set(retention) != ({"mode", field} if field else {"mode"}):
            raise LoggingPolicyError("invalid_policy", "Invalid retention fields")
        if field and (isinstance(retention[field], bool) or not isinstance(retention[field], int) or not 1 <= retention[field] <= 2**53 - 1):
            raise LoggingPolicyError("invalid_policy", "Invalid retention bound")
    if result["timezone"] != "local":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(result["timezone"])
        except (TypeError, ZoneInfoNotFoundError):
            raise LoggingPolicyError("invalid_policy", "Invalid timezone") from None
    if result["initial_period"] not in {"7d", "30d", "90d", "all"}:
        raise LoggingPolicyError("invalid_policy", "Invalid initial period")
    if not isinstance(result["saved_filters"], dict):
        raise LoggingPolicyError("invalid_policy", "Filters must be an object")
    from services.logging.projection import normalize_filters
    result["saved_filters"] = normalize_filters(result["saved_filters"])
    return result


class LoggingPolicyStore:
    def __init__(self, *, loader=None, saver=None):
        self.loader = loader
        self.saver = saver
        self.pins = {}
        self.completed = {}

    @staticmethod
    def _owner(owner):
        value = str(owner or "").strip().lower()
        if not value:
            raise LoggingPolicyError("owner_required", "Trusted logging owner required")
        return value

    def _load(self, owner):
        if self.loader:
            return self.loader(owner)
        from routes.prefs_routes import _load_for_user
        from src.owner_identity import LOCAL_INSTALLATION_OWNER
        return _load_for_user(None if owner == LOCAL_INSTALLATION_OWNER else owner)

    def _save(self, owner, value):
        if self.saver:
            return self.saver(owner, value)
        from routes.prefs_routes import _save_for_user
        from src.owner_identity import LOCAL_INSTALLATION_OWNER
        return _save_for_user(None if owner == LOCAL_INSTALLATION_OWNER else owner, value, allow_logging=True)

    def get_policy(self, owner):
        owner = self._owner(owner)
        with PREFERENCE_LOCK:
            prefs = self._load(owner)
            return normalize(prefs.get("logging_preferences") if isinstance(prefs, dict) else None)

    def update_policy(self, owner, patch, *, expected_revision):
        owner = self._owner(owner)
        if not isinstance(patch, dict) or {"revision", "schema"} & set(patch):
            raise LoggingPolicyError("invalid_policy", "Patch cannot select revision/schema")
        with PREFERENCE_LOCK:
            current = self.get_policy(owner)
            if isinstance(expected_revision, bool) or expected_revision != current["revision"]:
                raise LoggingPolicyError("revision_conflict", "Logging policy changed; reload settings")
            updated = normalize({**current, **patch, "revision": current["revision"] + 1})
            prefs = self._load(owner)
            prefs = dict(prefs) if isinstance(prefs, dict) else {}
            prefs["logging_preferences"] = updated
            self._save(owner, prefs)
            return copy.deepcopy(updated)

    def pin_policy(self, owner, operation_id, *, holder_id="host", incognito=False):
        owner = self._owner(owner)
        if not operation_id or not holder_id:
            raise LoggingPolicyError("invalid_operation", "Operation identity required")
        key = (owner, operation_id)
        with PREFERENCE_LOCK:
            if key not in self.pins:
                if len(self.pins) >= 10000:
                    raise LoggingPolicyError("pin_capacity", "Logging operation capacity is full")
                policy = self.get_policy(owner)
                self.pins[key] = {"policy": policy, "holders": set(), "private": False}
            # Privacy suppression wins even if an existing pin predates the
            # explicit nonpersistent admission.
            if incognito:
                self.pins[key]["private"] = True
            self.pins[key]["holders"].add(holder_id)
            return copy.deepcopy(self.pins[key]["policy"])

    def private_operation(self, owner, operation_id):
        with PREFERENCE_LOCK:
            entry = self.pins.get((self._owner(owner), operation_id))
            if entry:
                return bool(entry["private"])
            completed = self.completed.get((self._owner(owner), operation_id))
            return bool(completed and completed[1] > time.monotonic() and completed[2])

    def release_pin(self, owner, operation_id, *, holder_id="host"):
        with PREFERENCE_LOCK:
            key = (self._owner(owner), operation_id)
            entry = self.pins.get(key)
            if entry:
                entry["holders"].discard(holder_id)
                if not entry["holders"]:
                    self.completed[key] = (copy.deepcopy(entry["policy"]), time.monotonic()+3600, entry["private"])
                    self.completed = {k: v for k, v in self.completed.items() if v[1] > time.monotonic()}
                    if len(self.completed) > 10000:
                        self.completed.pop(next(iter(self.completed)))
                    self.pins.pop(key, None)

    def release_holder(self, owner, holder_id):
        with PREFERENCE_LOCK:
            for key, entry in list(self.pins.items()):
                if key[0] == self._owner(owner):
                    entry["holders"].discard(holder_id)
                    if not entry["holders"]:
                        self.pins.pop(key, None)

    def logging_status(self, owner):
        owner = self._owner(owner)
        policy = self.get_policy(owner)
        with PREFERENCE_LOCK:
            pins = [entry["policy"] for (o, _), entry in self.pins.items() if o == owner]
            old = sum(p["revision"] != policy["revision"] for p in pins)
        return {"scope": "open_clank_only", "ordinary": {"state": "always_on"},
                "advanced_enabled": policy["advanced_enabled"],
                "transport_mode": "advanced_proxy" if policy["advanced_enabled"] else "direct",
                "policy_revision": policy["revision"], "inflight_operations": len(pins),
                "old_policy_inflight": old, "draining": bool(old), "retention_execution": "on_capture_maintenance",
                "binary_body_capability": {"state": "supported", "formats": ["json_embedded_base64", "data_url"], "limits": "bounded_body_only_no_url_fetch_or_multipart"}}


_STORE = LoggingPolicyStore()


def get_policy(owner):
    return _STORE.get_policy(owner)


def update_policy(owner, patch, *, expected_revision):
    return _STORE.update_policy(owner, patch, expected_revision=expected_revision)


def pin_policy(owner, operation_id, **kwargs):
    return _STORE.pin_policy(owner, operation_id, **kwargs)


