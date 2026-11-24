"""User-adjustable ceilings for Memery recall. Existing authority still applies."""
DEFAULTS = {
    "memery_prompt_recall_enabled": True,
    "memery_prompt_recall_limit": 3,
    "memery_prompt_recall_chars": 1200,
    "memery_agent_search_enabled": True,
}


def validate_preference(key, value):
    if key not in DEFAULTS:
        raise ValueError("Unknown Memery preference")
    maximum = DEFAULTS[key]
    if type(maximum) is bool:
        if type(value) is not bool:
            raise ValueError(f"{key} requires a boolean")
    elif type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{key} must be an integer from 1 through {maximum}")
    return value


def normalize_preferences(prefs):
    """Missing fields inherit defaults. Invalid stored fields disable the affected operation."""
    result = dict(DEFAULTS)
    if not isinstance(prefs, dict):
        return {**result, "memery_prompt_recall_enabled": False, "memery_agent_search_enabled": False}
    for key in DEFAULTS:
        try:
            if key in prefs:
                result[key] = validate_preference(key, prefs[key])
        except (ValueError, TypeError):
            result["memery_agent_search_enabled" if key == "memery_agent_search_enabled" else "memery_prompt_recall_enabled"] = False
    return result


def owner_preferences(owner):
    from routes.prefs_routes import _load_for_user
    try:
        # Writers atomically replace the file. A read needs no exclusive writer lock.
        return normalize_preferences(_load_for_user(owner))
    except (OSError, TypeError, ValueError):
        return {**DEFAULTS, "memery_prompt_recall_enabled": False, "memery_agent_search_enabled": False}
