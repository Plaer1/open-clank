"""Serialized access to Cookbook state and its private acquisition reservations."""
import copy
import json
import re
from contextlib import contextmanager
from pathlib import Path

from core.atomic_io import _path_locks, atomic_write_json

PRIVATE_KEY = "_pinned_acquisitions_v1"
_STATES = {"prepared", "dispatching", "running", "complete", "failed", "unknown"}


class CookbookStateError(ValueError):
    pass


def _validate(state):
    if not isinstance(state, dict):
        raise CookbookStateError("invalid Cookbook state")
    if any(isinstance(key, str) and key.startswith("_pinned_acquisitions_")
           and key != PRIVATE_KEY for key in state):
        raise CookbookStateError("unrecognized acquisition state")
    records = state.get(PRIVATE_KEY, {})
    if not isinstance(records, dict) or len(records) > 1024:
        raise CookbookStateError("invalid acquisition reservations")
    for key, record in records.items():
        if (not isinstance(key, str) or not 1 <= len(key) <= 128
                or not isinstance(record, dict)
                or not ({"account_id", "digest", "revision", "state"} <= set(record))
                or not set(record) <= {"account_id", "digest", "revision", "state", "review", "owner", "receipt", "cache"}
                or (("review" in record) != ("owner" in record))
                or not isinstance(record["account_id"], str)
                or not 1 <= len(record["account_id"]) <= 256
                or not isinstance(record["digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["digest"])
                or type(record["revision"]) is not int or not 1 <= record["revision"] <= 2**53 - 1
                or not isinstance(record["state"], str) or record["state"] not in _STATES):
            raise CookbookStateError("invalid acquisition reservation")
        if "cache" in record:
            cache = record["cache"]
            if (not isinstance(cache, dict) or cache.get("phase") not in {"intent", "created"}
                    or set(cache) != ({"phase", "name"} if cache.get("phase") == "intent" else {"phase", "name", "device", "inode"})
                    or not isinstance(cache.get("name"), str) or not re.fullmatch(r"acquisition-[0-9a-f]{32}", cache["name"])
                    or (cache.get("phase") == "created" and (type(cache["device"]) is not int or type(cache["inode"]) is not int or cache["inode"] <= 0))):
                raise CookbookStateError("invalid acquisition cache")
        if "receipt" in record:
            receipt = record["receipt"]
            if (record["state"] not in {"complete", "failed"} or not isinstance(receipt, dict)
                    or set(receipt) != {"status", "files"} or receipt["status"] != record["state"]
                    or not isinstance(receipt["files"], list) or len(receipt["files"]) > 100
                    or any(not isinstance(f, dict) or set(f) != {"path", "size", "sha256"}
                           or not isinstance(f["path"], str) or type(f["size"]) is not int
                           or f["size"] < 0 or not isinstance(f["sha256"], str)
                           or not re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in receipt["files"])):
                raise CookbookStateError("invalid acquisition receipt")
        if "review" in record:
            if not isinstance(record.get("owner"), str) or not record["owner"] or record["owner"] != record["owner"].strip().lower() or len(record["owner"]) > 256:
                raise CookbookStateError("invalid acquisition owner alias")
            from src.openclank.cookbook_acquisition import validate_review
            try:
                if validate_review(record["review"]) != record["digest"]:
                    raise ValueError("digest mismatch")
            except (TypeError, ValueError, KeyError, UnicodeError) as exc:
                raise CookbookStateError("invalid acquisition review") from exc


@contextmanager
def transaction(path):
    path = Path(path)
    # Distinct coordination key: atomic_write_json locks the target itself.
    with _path_locks([str(path) + ".cookbook-transaction"]):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError) as exc:
            raise CookbookStateError("Cookbook state unavailable") from exc
        _validate(state)
        yield state


def save(path, state):
    """Save only while holding transaction(path)."""
    _validate(state)
    atomic_write_json(str(path), state, indent=2)


def client_state(state):
    result = copy.deepcopy(state)
    result.pop(PRIVATE_KEY, None)
    return result


def reserve(state, *, operation_id, account_id, digest, review=None, owner=None):
    records = state.setdefault(PRIVATE_KEY, {})
    candidate = {"account_id": account_id, "digest": digest, "revision": 1, "state": "prepared"}
    if review is not None:
        candidate["review"] = copy.deepcopy(review)
        candidate["owner"] = owner
    existing = records.get(operation_id)
    if existing is not None:
        if existing["account_id"] != account_id or existing["digest"] != digest or existing.get("review") != review or existing.get("owner") != owner:
            raise CookbookStateError("reservation identity conflict")
        return copy.deepcopy(existing)
    records[operation_id] = candidate
    _validate(state)
    return copy.deepcopy(candidate)


def advance(state, *, operation_id, account_id, digest, revision, expected, target):
    record = state.get(PRIVATE_KEY, {}).get(operation_id)
    allowed = {("prepared", "dispatching"), ("dispatching", "running"),
               ("dispatching", "unknown"), ("running", "complete"),
               ("running", "failed"), ("running", "unknown"),
               ("unknown", "running"), ("unknown", "complete"), ("unknown", "failed")}
    if (not record or record["account_id"] != account_id or record["digest"] != digest
            or record["revision"] != revision or record["state"] != expected
            or (expected, target) not in allowed):
        raise CookbookStateError("reservation compare-and-swap failed")
    record["revision"] += 1
    record["state"] = target
    _validate(state)
    return copy.deepcopy(record)


def merge_orphan_tasks(state, observed):
    """Merge a completed outside-lock observation into the current state."""
    tasks = state.get("tasks") or []
    if not isinstance(tasks, list):
        raise CookbookStateError("invalid task list")
    removed = state.get("removedTasks") or {}
    if not isinstance(removed, dict):
        raise CookbookStateError("invalid removed tasks")
    known = {t.get("sessionId") for t in tasks if isinstance(t, dict)}
    for task in observed:
        if not isinstance(task, dict) or not task.get("_adoptedExternally"):
            continue
        sid = task.get("sessionId")
        if sid and sid not in known and sid not in removed:
            tasks.append(task)
            known.add(sid)
    state["tasks"] = tasks


def subject_inventory(state, account_id):
    """Private exact-subject snapshot; unfinished work blocks lifecycle changes."""
    if not isinstance(account_id, str) or not account_id:
        raise CookbookStateError("immutable account required")
    records = state.get(PRIVATE_KEY, {})
    owned = {key: copy.deepcopy(value) for key, value in records.items()
             if value["account_id"] == account_id}
    if any(value["state"] in {"dispatching", "running", "unknown"} for value in owned.values()):
        raise CookbookStateError("acquisition requires reconciliation")
    return owned


def purge_subject(state, account_id, expected):
    """Remove only a frozen terminal/prepared subject inventory."""
    current = subject_inventory(state, account_id)
    if current != expected:
        raise CookbookStateError("acquisition inventory changed")
    for key in current:
        del state[PRIVATE_KEY][key]
    return len(current)


def owned_rows(state, owner, account_id):
    if not account_id:
        raise CookbookStateError("immutable account required")
    result = {}
    for key, row in state.get(PRIVATE_KEY, {}).items():
        if row["account_id"] != account_id:
            continue
        if "owner" not in row:
            raise CookbookStateError("acquisition owner alias unavailable")
        if row["owner"] == owner:
            if row["state"] in {"dispatching", "running", "unknown"}:
                raise CookbookStateError("acquisition requires reconciliation")
            result[key] = copy.deepcopy(row)
    return result


def row_tokens(rows):
    import hashlib
    return sorted(key + ':' + hashlib.sha256(json.dumps(
        {k:v for k,v in row.items() if k != 'owner'}, sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        for key, row in rows.items())


def finish_verified(state, *, operation_id, account_id, digest, revision, receipt):
    record = state.get(PRIVATE_KEY, {}).get(operation_id)
    if (not record or record["account_id"] != account_id or record["digest"] != digest
            or record["revision"] != revision or record["state"] != "running"
            or receipt.get("status") not in {"complete", "failed"}):
        raise CookbookStateError("terminal compare-and-swap failed")
    record["revision"] += 1
    record["state"] = receipt["status"]
    record["receipt"] = copy.deepcopy(receipt)
    _validate(state)
