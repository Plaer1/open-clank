"""Pure, owner-scoped Stats alert preference and crossing logic.

This module only produces safe notification candidates.  The browser or native
shell still owns permission and delivery, then acknowledges delivered tokens so
the persisted owner preference can suppress duplicates across restarts.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from services.stats.privacy import owner_scope


DEFAULT_STATS_PREFERENCES = {
    "schema": "open-clank.stats.preferences.v1",
    "refresh_seconds": 300,
    "diagnostics": False,
    "cost_mode": "tokens",
    "quota_alerts": False,
    "quota_thresholds": [80, 90, 100],
    "include_internal": False,
    "include_probes": False,
}

_SAFE_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "openrouter": "OpenRouter",
}


class StatsAlertError(ValueError):
    pass


def normalize_preferences(value: object) -> dict:
    raw = dict(value) if isinstance(value, dict) else {}
    result = dict(DEFAULT_STATS_PREFERENCES)
    refresh = raw.get("refresh_seconds", result["refresh_seconds"])
    if isinstance(refresh, bool) or not isinstance(refresh, int):
        raise StatsAlertError("refresh_seconds must be an integer")
    result["refresh_seconds"] = min(86_400, max(30, refresh))
    for key in ("diagnostics", "quota_alerts", "include_internal", "include_probes"):
        if key in raw and not isinstance(raw[key], bool):
            raise StatsAlertError(f"{key} must be a boolean")
        result[key] = raw.get(key, result[key])
    mode = raw.get("cost_mode", result["cost_mode"])
    if mode not in {"tokens", "cost"}:
        raise StatsAlertError("cost_mode must be tokens or cost")
    result["cost_mode"] = mode
    thresholds = raw.get("quota_thresholds", result["quota_thresholds"])
    if (not isinstance(thresholds, list) or not thresholds
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 or item > 100 for item in thresholds)):
        raise StatsAlertError("quota_thresholds must contain percentages from 1 to 100")
    result["quota_thresholds"] = sorted(set(thresholds))[:10]
    return result


def _utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")).astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None
    return None


def _window_key(owner: str, row: dict) -> str:
    raw = "\0".join(str(row.get(key) or "") for key in ("account_id", "provider_id", "window_id"))
    return hashlib.sha256(f"stats-alert-window-v1\0{owner}\0{raw}".encode()).hexdigest()[:24]


def _cycle_key(row: dict) -> str:
    reset = _utc(row.get("reset_at"))
    return reset.isoformat() if reset else "continuous-or-unknown"


def evaluate_quota_alerts(*, owner: str, observations: list[dict], preferences: object,
                          state: object = None, now: datetime | None = None) -> tuple[list[dict], dict]:
    prefs = normalize_preferences(preferences)
    moment = _utc(now) or datetime.now(timezone.utc)
    previous = dict(state) if isinstance(state, dict) else {}
    windows = dict(previous.get("windows") or {})
    candidates: list[dict] = []

    for row in observations[:10_000]:
        if not isinstance(row, dict):
            continue
        key = _window_key(owner, row)
        prior = dict(windows.get(key) or {})
        numerator, denominator = row.get("utilization_numerator"), row.get("utilization_denominator")
        observed = _utc(row.get("observed_at"))
        authoritative = (
            row.get("state") == "official"
            and isinstance(numerator, int) and not isinstance(numerator, bool) and numerator >= 0
            and isinstance(denominator, int) and not isinstance(denominator, bool) and denominator > 0
            and observed is not None and observed <= moment
        )
        if not authoritative:
            continue
        percent = numerator * 100 / denominator
        cycle = _cycle_key(row)
        if prior.get("cycle") != cycle:
            prior = {"cycle": cycle, "last_percent": None, "delivered": [], "pending": []}
        old_percent = prior.get("last_percent")
        delivered = {int(item) for item in prior.get("delivered", []) if isinstance(item, int)}
        pending = {int(item) for item in prior.get("pending", []) if isinstance(item, int)}
        crossed = [threshold for threshold in prefs["quota_thresholds"]
                   if threshold not in delivered and threshold not in pending and percent >= threshold
                   and (old_percent is None or old_percent < threshold)]
        prior["last_percent"] = percent
        if prefs["quota_alerts"]:
            pending.update(crossed)
        else:
            pending.clear()
        prior["pending"] = sorted(pending)
        windows[key] = prior
        if not prefs["quota_alerts"]:
            continue
        provider = _SAFE_PROVIDER_LABELS.get(str(row.get("provider_id") or "").lower(), "Provider")
        window = str(row.get("window_kind") or "quota").lower()
        window_label = window.capitalize() if window in {"continuous", "discrete", "credits"} else "Quota"
        for threshold in sorted(pending):
            token = hashlib.sha256(f"stats-alert-v1\0{owner}\0{key}\0{cycle}\0{threshold}".encode()).hexdigest()[:32]
            candidates.append({
                "token": token,
                "window_handle": key,
                "owner_scope": owner_scope(owner),
                "provider_label": provider,
                "window_label": window_label,
                "threshold": threshold,
                "percent": round(percent, 2),
            })
    return candidates, {"schema": "open-clank.stats.alert-state.v1", "windows": windows}


def acknowledge_alerts(state: object, candidates: list[dict], tokens: list[str], *, owner: str) -> dict:
    result = dict(state) if isinstance(state, dict) else {}
    windows = {key: dict(value) for key, value in dict(result.get("windows") or {}).items()}
    accepted = {str(token) for token in tokens[:100]}
    by_token = {
        str(candidate.get("token")): (str(candidate.get("window_handle")), int(candidate.get("threshold")))
        for candidate in candidates if isinstance(candidate, dict)
        and isinstance(candidate.get("threshold"), int)
        and str(candidate.get("window_handle")) in windows
        and (owner is None or candidate.get("owner_scope") == owner_scope(owner))
    }
    for token in accepted:
        target = by_token.get(token)
        if target is None:
            continue
        key, threshold = target
        row = windows[key]
        delivered = {int(item) for item in row.get("delivered", []) if isinstance(item, int)}
        pending = {int(item) for item in row.get("pending", []) if isinstance(item, int)}
        delivered.add(threshold)
        pending.discard(threshold)
        row["delivered"] = sorted(delivered)
        row["pending"] = sorted(pending)
    result.update({"schema": "open-clank.stats.alert-state.v1", "windows": windows})
    return result
