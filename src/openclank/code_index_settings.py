"""Strict app-owned indexing preference; readback never cold-starts an engine."""

from copy import deepcopy
import asyncio

MAX_REVISION = (1 << 53) - 1
SCHEMA = "open-clank.index-scheduler/v1"
THREADS_KEY = "code_index_max_threads"
REVISION_KEY = "code_index_settings_revision"
LIMIT_KEYS = {"max_files": "code_index_max_files", "max_source_bytes": "code_index_max_source_bytes", "max_file_bytes": "code_index_max_file_bytes", "max_generation_bytes": "code_index_max_generation_bytes"}
MAX_INDEXED_FILES = 75_000
LEGACY_MAX_INDEXED_FILES = 10_000
MiB = 1024 * 1024
GiB = 1024 * MiB
HARD_LIMITS = {"max_files": MAX_INDEXED_FILES, "max_source_bytes": GiB, "max_file_bytes": 16 * MiB, "max_generation_bytes": 32 * GiB}
READ_ONLY_LIMITS = {"max_queued_jobs": 32, "max_descriptor_bytes": 64 * 1024,
                    "max_retained_data_bytes": 256 * 1024 * 1024, "max_generation_disk_bytes": HARD_LIMITS["max_generation_bytes"],
                    "max_source_records": 16000000, "max_resolution_records": 16000000}
_LEGACY_RECORD_LIMITS = {**READ_ONLY_LIMITS, "max_source_records": 4000000, "max_resolution_records": 4000000}
DEFAULT_LIMITS = {"max_files": MAX_INDEXED_FILES, "max_source_bytes": 256 * MiB, "max_file_bytes": 4 * MiB, "max_generation_bytes": 8 * GiB}
_PREVIOUS_DEFAULT = {"max_files": MAX_INDEXED_FILES, "max_source_bytes": 64 * MiB, "max_file_bytes": MiB // 2, "max_generation_bytes": 2 * GiB}
MANAGED_DEFAULTS = {THREADS_KEY: 2, **{key: DEFAULT_LIMITS[name] for name, key in LIMIT_KEYS.items()}}
MANAGED_KEYS = tuple(MANAGED_DEFAULTS)


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def validate_limits(value):
    if not isinstance(value, dict) or set(value) != set(HARD_LIMITS):
        raise ValueError("Invalid code indexing source limits")
    limits = {key: integer(value[key], key, 1, maximum) for key, maximum in HARD_LIMITS.items()}
    if limits["max_generation_bytes"] < 4096 or limits["max_generation_bytes"] % 4096:
        raise ValueError("Generation bytes must be a positive multiple of 4096")
    if limits["max_file_bytes"] > limits["max_source_bytes"]:
        raise ValueError("Per-file bytes must not exceed total source bytes")
    return limits


def upgrade_unsaved_defaults(settings):
    """Revision zero is factory configuration. Preserve every explicitly saved preference."""
    if settings.get(REVISION_KEY, 0) != 0 or settings.get(THREADS_KEY, 2) != 2:
        return settings
    for count in (MAX_INDEXED_FILES, LEGACY_MAX_INDEXED_FILES):
        previous = {**_PREVIOUS_DEFAULT, "max_files": count}
        if all(settings.get(key, previous[name]) == previous[name] for name, key in LIMIT_KEYS.items()):
            return {**settings, **{key: DEFAULT_LIMITS[name] for name, key in LIMIT_KEYS.items()}}
    return settings


def configured_limits(settings):
    return validate_limits({name: settings.get(key, DEFAULT_LIMITS[name]) for name, key in LIMIT_KEYS.items()})


def legacy_default_limits(desired, revision, limits):
    return revision == 0 and desired == 2 and limits == {**DEFAULT_LIMITS, "max_files": LEGACY_MAX_INDEXED_FILES}


def configured(settings):
    desired = integer(settings.get(THREADS_KEY, 2), THREADS_KEY, 1, 32)
    revision = integer(settings.get(REVISION_KEY, 0), REVISION_KEY, 0, MAX_REVISION)
    limits = configured_limits(settings)
    if revision == 0 and (desired != 2 or limits != DEFAULT_LIMITS) and not legacy_default_limits(desired, revision, limits):
        raise ValueError("Indexing settings need a saved revision")
    return desired, revision


def prepare_settings(current, patch):
    """Pure transaction preparation. Caller serializes load/prepare/save."""
    if REVISION_KEY in patch:
        raise ValueError("Indexing settings revision is read-only")
    value = deepcopy(current)
    value.update(deepcopy(patch))
    integer(value.get(THREADS_KEY, 2), THREADS_KEY, 1, 32)
    configured_limits(value)
    if any(key in patch and (type(current.get(key, default)) is not int or patch[key] != current.get(key, default))
           for key, default in MANAGED_DEFAULTS.items()):
        revision = integer(current.get(REVISION_KEY, 0), REVISION_KEY, 0, MAX_REVISION)
        if revision == MAX_REVISION:
            raise ValueError("Indexing settings revision exhausted")
        value[REVISION_KEY] = revision + 1
    configured(value)
    return value


def configuration_request(settings):
    desired, revision = configured(settings)
    limits = configured_limits(settings)
    if revision or legacy_default_limits(desired, revision, limits):
        return {"action": "configure", "revision": revision, "max_threads": desired, "limits": limits}
    return {"action": "status"}


def preserve_scheduler_revision(incoming, persisted):
    """Unrelated stale whole-settings writers cannot roll back this preference."""
    value = deepcopy(incoming)
    persisted = upgrade_unsaved_defaults(persisted)
    incoming = upgrade_unsaved_defaults(incoming) if any(key in incoming for key in MANAGED_KEYS) else incoming
    value.update(incoming)
    previous = integer(persisted.get(REVISION_KEY, 0), REVISION_KEY, 0, MAX_REVISION)
    if not any(key in incoming for key in (*MANAGED_KEYS, REVISION_KEY)):
        for key in (*MANAGED_KEYS, REVISION_KEY):
            if key in persisted:
                value[key] = persisted[key]
        return value
    for key in MANAGED_KEYS:
        if key not in value and key in persisted:
            value[key] = persisted[key]
    revision = integer(incoming.get(REVISION_KEY, 0), REVISION_KEY, 0, MAX_REVISION)
    if revision < previous:
        for key in MANAGED_KEYS:
            value[key] = persisted.get(key, MANAGED_DEFAULTS[key])
        value[REVISION_KEY] = previous
    elif revision == previous:
        if configured(value)[0] != configured(persisted)[0] or configured_limits(value) != configured_limits(persisted):
            raise ValueError("Indexing preference requires a new saved revision")
    elif revision != previous + 1:
        raise ValueError("Invalid indexing preference revision")
    configured(value)
    return value


def validate_status(value):
    keys = {"schema", "revision", "effective_revision", "desired", "effective",
            "active", "queued", "drain_pending", "desired_limits", "effective_limits", "hard_limits"}
    if not isinstance(value, dict) or set(value) != keys or value.get("schema") != SCHEMA:
        raise ValueError("Invalid indexing scheduler response")
    for key in ("revision", "effective_revision"):
        integer(value[key], key, 0, MAX_REVISION)
    for key in ("desired", "effective"):
        integer(value[key], key, 1, 32)
    desired_limits = validate_limits(value["desired_limits"])
    effective_limits = validate_limits(value["effective_limits"])
    if value["hard_limits"] not in ({**HARD_LIMITS, **READ_ONLY_LIMITS}, {**HARD_LIMITS, **_LEGACY_RECORD_LIMITS}):
        raise ValueError("Unsupported code indexing hard limits")
    integer(value["active"], "active", 0, value["effective"])
    integer(value["queued"], "queued", 0, 32)
    if (type(value["drain_pending"]) is not bool
            or value["effective_revision"] > value["revision"]
            or value["drain_pending"] != (value["effective_revision"] != value["revision"])
            or (not value["drain_pending"] and
                (value["effective_revision"] != value["revision"] or value["effective"] != value["desired"] or effective_limits != desired_limits))):
        raise ValueError("Inconsistent indexing scheduler response")
    return dict(value)


async def readback(provider, settings, *, apply=False):
    """No paths, identities or raw exception text in global Settings output."""
    result = {"native_status": "unavailable", "status": "pending", "desired": None,
              "revision": None, "native_revision": None, "effective_revision": None,
              "effective": None, "active": None, "queued": None, "drain_pending": None,
              "desired_limits": None, "effective_limits": None, "hard_limits": None, "legacy_file_default": False}
    try:
        desired, revision = configured(settings)
        limits = configured_limits(settings)
        result.update(desired=desired, revision=revision, desired_limits=limits,
                      legacy_file_default=legacy_default_limits(desired, revision, limits))
        method = getattr(provider, "code_index_scheduler", None)
        if method is None:
            return result
        request = configuration_request(settings) if apply else {"action": "status"}
        # Bound a Settings observation, never the indexing job itself.
        value = validate_status(await asyncio.wait_for(method(**request, ready_only=True), timeout=1.5))
        acknowledged = value["revision"] == revision and value["desired"] == desired and value["desired_limits"] == result["desired_limits"]
        result.update(native_status="available", native_revision=value["revision"],
                      **{k: value[k] for k in ("effective_revision", "effective", "active", "queued", "drain_pending", "effective_limits", "hard_limits")})
        result["status"] = ("draining" if value["drain_pending"] else "applied") if acknowledged else "pending"
    except Exception:
        # A valid saved preference stays saved/pending while the owner is closed,
        # old or unavailable. Cancellation still propagates (BaseException).
        pass
    return result
