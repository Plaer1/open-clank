"""Strict app-owned indexing preference; readback never cold-starts an engine."""

from copy import deepcopy
import asyncio

MAX_REVISION = (1 << 53) - 1
SCHEMA = "open-clank.index-scheduler/v1"
THREADS_KEY = "code_index_max_threads"
REVISION_KEY = "code_index_settings_revision"


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def configured(settings):
    desired = integer(settings.get(THREADS_KEY, 2), THREADS_KEY, 1, 32)
    revision = integer(settings.get(REVISION_KEY, 0), REVISION_KEY, 0, MAX_REVISION)
    if revision == 0 and desired != 2:
        raise ValueError("Indexing settings need a saved revision")
    return desired, revision


def prepare_settings(current, patch):
    """Pure transaction preparation. Caller serializes load/prepare/save."""
    if REVISION_KEY in patch:
        raise ValueError("Indexing settings revision is read-only")
    value = deepcopy(current)
    if THREADS_KEY in patch:
        desired = integer(patch[THREADS_KEY], THREADS_KEY, 1, 32)
        revision = integer(current.get(REVISION_KEY, 0), REVISION_KEY, 0, MAX_REVISION)
        if type(current.get(THREADS_KEY, 2)) is not int or desired != current.get(THREADS_KEY, 2):
            if revision == MAX_REVISION:
                raise ValueError("Indexing settings revision exhausted")
            value[REVISION_KEY] = revision + 1
    value.update(deepcopy(patch))
    return value


def configuration_request(settings):
    desired, revision = configured(settings)
    return ({"action": "configure", "revision": revision, "max_threads": desired}
            if revision else {"action": "status"})


def preserve_scheduler_revision(incoming, persisted):
    """Unrelated stale whole-settings writers cannot roll back this preference."""
    value = deepcopy(incoming)
    previous = integer(persisted.get(REVISION_KEY, 0), REVISION_KEY, 0, MAX_REVISION)
    if THREADS_KEY not in incoming and REVISION_KEY not in incoming:
        for key in (THREADS_KEY, REVISION_KEY):
            if key in persisted:
                value[key] = persisted[key]
        return value
    revision = integer(incoming.get(REVISION_KEY, 0), REVISION_KEY, 0, MAX_REVISION)
    if revision < previous:
        value.update({THREADS_KEY: persisted.get(THREADS_KEY, 2), REVISION_KEY: previous})
    elif revision == previous:
        if incoming.get(THREADS_KEY, 2) != persisted.get(THREADS_KEY, 2):
            raise ValueError("Indexing preference requires a new saved revision")
        configured(value)
    else:
        if revision != previous + 1:
            raise ValueError("Invalid indexing preference revision")
        configured(value)
    return value


def validate_status(value):
    keys = {"schema", "revision", "effective_revision", "desired", "effective",
            "active", "queued", "drain_pending"}
    if not isinstance(value, dict) or set(value) != keys or value.get("schema") != SCHEMA:
        raise ValueError("Invalid indexing scheduler response")
    for key in ("revision", "effective_revision"):
        integer(value[key], key, 0, MAX_REVISION)
    for key in ("desired", "effective"):
        integer(value[key], key, 1, 32)
    integer(value["active"], "active", 0, value["effective"])
    integer(value["queued"], "queued", 0, 32)
    if (type(value["drain_pending"]) is not bool
            or value["effective_revision"] > value["revision"]
            or (not value["drain_pending"] and
                (value["effective_revision"] != value["revision"] or value["effective"] != value["desired"]))):
        raise ValueError("Inconsistent indexing scheduler response")
    return dict(value)


async def readback(provider, settings, *, apply=False):
    """No paths, identities or raw exception text in global Settings output."""
    result = {"native_status": "unavailable", "status": "pending", "desired": None,
              "revision": None, "native_revision": None, "effective_revision": None,
              "effective": None, "active": None, "queued": None, "drain_pending": None}
    try:
        desired, revision = configured(settings)
        result.update(desired=desired, revision=revision)
        method = getattr(provider, "code_index_scheduler", None)
        if method is None:
            return result
        request = configuration_request(settings) if apply else {"action": "status"}
        # Bound a Settings observation, never the indexing job itself.
        value = validate_status(await asyncio.wait_for(method(**request, ready_only=True), timeout=1.5))
        acknowledged = value["revision"] == revision and value["desired"] == desired
        result.update(native_status="available", native_revision=value["revision"],
                      **{k: value[k] for k in ("effective_revision", "effective", "active", "queued", "drain_pending")})
        result["status"] = ("draining" if value["drain_pending"] else "applied") if acknowledged else "pending"
    except Exception:
        # A valid saved preference stays saved/pending while the owner is closed,
        # old or unavailable. Cancellation still propagates (BaseException).
        pass
    return result
