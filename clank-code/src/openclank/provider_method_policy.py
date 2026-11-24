"""Owner-scoped Claude subscription Beta policy, default On.

Prefs lock precedes provider DB transactions. Never hold it during engine IPC.
"""
from __future__ import annotations

import copy
from typing import Any
from src.owner_identity import LOCAL_INSTALLATION_OWNER

KEY = "provider_method_policy"
SCHEMA = "openclank.provider-method-policy.v1"
METHOD = "anthropic.claude_subscription"


class ProviderMethodPolicyError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _owner(owner: str) -> str:
    if type(owner) is not str or not owner.strip():
        raise ProviderMethodPolicyError("owner_required", "Provider owner is required")
    return owner.strip().lower()


def _stored(value: Any) -> dict[str, Any]:
    if value is None:
        return {"schema":SCHEMA, "revision":1, "methods":{METHOD:{"enabled":True}}}
    if (type(value) is not dict or set(value) != {"schema","revision","methods"} or value.get("schema") != SCHEMA or
        type(value.get("revision")) is not int or value["revision"] < 1 or type(value.get("methods")) is not dict or
        set(value["methods"]) != {METHOD} or type(value["methods"][METHOD]) is not dict or
        set(value["methods"][METHOD]) != {"enabled"} or type(value["methods"][METHOD]["enabled"]) is not bool):
        raise ProviderMethodPolicyError("invalid_stored_policy", "Provider method policy requires recovery")
    return copy.deepcopy(value)


def get_policy(owner: str) -> dict[str, Any]:
    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
    owner = _owner(owner)
    with PREFERENCE_LOCK:
        prefs = _load_for_user(None if owner == LOCAL_INSTALLATION_OWNER else owner)
        return _stored(prefs.get(KEY))


def set_policy(owner: str, *, enabled: bool, expected_revision: int) -> dict[str, Any]:
    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user, _save_for_user
    owner = _owner(owner)
    if type(enabled) is not bool or type(expected_revision) is not int or expected_revision < 0:
        raise ProviderMethodPolicyError("invalid_policy", "Boolean enablement and nonnegative expected revision are required")
    user = None if owner == LOCAL_INSTALLATION_OWNER else owner
    with PREFERENCE_LOCK:
        prefs = _load_for_user(user)
        current = _stored(prefs.get(KEY))
        if current["revision"] != expected_revision:
            raise ProviderMethodPolicyError("revision_conflict", "Provider method policy changed; refresh settings")
        result = {"schema":SCHEMA, "revision":expected_revision + 1, "methods":{METHOD:{"enabled":enabled}}}
        prefs[KEY] = result
        _save_for_user(user, prefs, allow_provider_method_policy=True)
        return copy.deepcopy(result)


def require_enabled(owner: str, expected_revision: int | None = None) -> int:
    policy = get_policy(owner)
    if not policy["methods"][METHOD]["enabled"]:
        raise ProviderMethodPolicyError("beta_disabled", "Claude subscription Beta is disabled")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision != policy["revision"]):
        raise ProviderMethodPolicyError("beta_policy_stale", "Claude subscription Beta policy changed")
    return policy["revision"]


def projection(owner: str) -> dict[str, Any]:
    policy = get_policy(owner)
    return {**policy, "managed_admission":{"available":True, "scope":"actor_execution", "requires_negotiated_contract":1},
            "auxiliary_admission":{"available":False, "reason":"auxiliary_profile_unavailable"},
            "disable_behavior":"cooperative_next_admission"}
