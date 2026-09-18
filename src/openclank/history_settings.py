"""Durable history policy and usage settings.

The Rust worker owns capture bytes and redb journals. This small Python
adapter owns the authenticated application settings projection so browser and
HTTP clients can inspect/update policy without inventing a second history
store. The file is replaced atomically and carries an expected revision.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping

DEFAULT_TOTAL_BYTES = 1_073_741_824
_LOCK = threading.RLock()


def settings_path() -> Path:
    return Path(
        os.environ.get(
            "OPENCLANK_HISTORY_SETTINGS_FILE",
            os.path.join(os.environ.get("OPENCLANK_DATA_DIR", "."), "history-settings.json"),
        )
    )


def history_root() -> Path:
    return Path(os.environ.get("OPENCLANK_HISTORY_ROOT", str(settings_path().parent / "history")))


def _default() -> dict[str, Any]:
    return {
        "revision": 1,
        "global": {"total_bytes": DEFAULT_TOTAL_BYTES, "enabled": True},
        "scopes": [],
        "health": {
            "state": "ready",
            "reason": None,
            "history_paused": False,
            "measured_at_millis": None,
        },
    }


def scope_id(
    kind: str,
    value: str,
    workspace_id: str | None = None,
    owner_account_id: str | None = None,
) -> str:
    """Return a stable server-derived scope id; callers cannot choose owner ids."""
    raw = "\0".join((kind.strip().lower(), owner_account_id or "", workspace_id or "", value.strip()))
    return f"{kind.strip().lower()}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _default()
    if not isinstance(value, dict):
        return _default()
    result = _default()
    result.update(value)
    result["global"] = {**_default()["global"], **(value.get("global") or {})}
    result["scopes"] = value.get("scopes") if isinstance(value.get("scopes"), list) else []
    result["health"] = {**_default()["health"], **(value.get("health") or {})}
    try:
        result["revision"] = max(1, int(result["revision"]))
    except (TypeError, ValueError):
        result["revision"] = 1
    return result


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_settings(path: Path | None = None) -> dict[str, Any]:
    with _LOCK:
        return _read(path or settings_path())


def _validate_scope(scope: Mapping[str, Any], owner_account_id: str | None = None) -> dict[str, Any]:
    kind = str(scope.get("kind") or "").strip().lower()
    if kind not in {"workspace", "directory"}:
        raise ValueError("scope kind must be workspace or directory")
    workspace_id = str(scope.get("workspace_id") or "").strip() or None
    owner = str(scope.get("owner_account_id") or owner_account_id or "").strip() or None
    if not owner:
        raise ValueError("non-global scope requires an owner_account_id")
    if kind == "workspace" and not workspace_id:
        raise ValueError("workspace scope requires a workspace_id")
    value = str(scope.get("value") or scope.get("root") or workspace_id or "").strip()
    if not value:
        raise ValueError("scope value is required")
    if kind == "directory":
        value = str(Path(value).expanduser().resolve())
    try:
        limit = int(scope["limit_bytes"]) if scope.get("limit_bytes") is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("limit_bytes must be an integer") from exc
    if limit is not None and limit <= 0:
        raise ValueError("limit_bytes must be positive")
    return {
        "scope_id": scope_id(kind, value, workspace_id, owner),
        "kind": kind,
        "owner_account_id": owner,
        "workspace_id": workspace_id,
        "value": value,
        "limit_bytes": limit,
        "enabled": bool(scope.get("enabled", True)),
    }


def update_settings(
    patch: Mapping[str, Any],
    *,
    expected_revision: int,
    path: Path | None = None,
    allow_global: bool = True,
) -> dict[str, Any]:
    target = path or settings_path()
    with _LOCK:
        current = _read(target)
        if int(current["revision"]) != int(expected_revision):
            raise ValueError("history settings revision conflict")
        result = dict(current)
        if "global" in patch:
            if not allow_global:
                raise PermissionError("installation history target requires administration")
            global_patch = patch["global"]
            if not isinstance(global_patch, Mapping):
                raise ValueError("global settings must be an object")
            total = int(global_patch.get("total_bytes", current["global"]["total_bytes"]))
            if total <= 0:
                raise ValueError("total_bytes must be positive")
            result["global"] = {
                "total_bytes": total,
                "enabled": bool(global_patch.get("enabled", current["global"].get("enabled", True))),
            }
        if "scopes" in patch:
            scopes = patch["scopes"]
            if not isinstance(scopes, list) or len(scopes) > 256:
                raise ValueError("scopes must be a list of at most 256 entries")
            normalized = [_validate_scope(scope) for scope in scopes if isinstance(scope, Mapping)]
            if len(normalized) != len(scopes) or len({item["scope_id"] for item in normalized}) != len(normalized):
                raise ValueError("scopes must be unique objects")
            result["scopes"] = normalized
        result["revision"] = int(current["revision"]) + 1
        _atomic_write(target, result)
        return result


def measure_usage(root: Path | None = None) -> dict[str, Any]:
    target = root or history_root()
    apparent = 0
    allocated = 0
    count = 0
    if target.exists():
        for path in target.rglob("*"):
            try:
                stat = path.lstat()
            except OSError:
                continue
            if not path.is_file():
                continue
            count += 1
            apparent += int(stat.st_size)
            allocated += int(getattr(stat, "st_blocks", 0) * 512) or int(stat.st_size)
    return {
        "logical_retained_bytes": apparent,
        "physical_allocated_bytes": allocated,
        "apparent_file_bytes": apparent,
        "reserved_inflight_bytes": 0,
        "reclaimable_estimate_bytes": 0,
        "retained_version_count": count,
        "measured_at_millis": int(time.time() * 1000),
        "measurement_quality": "allocated" if allocated else "apparent",
        "scopes": [],
    }


def snapshot(path: Path | None = None, root: Path | None = None) -> dict[str, Any]:
    settings = load_settings(path)
    usage = measure_usage(root)
    health = dict(settings.get("health") or {})
    health.setdefault("state", "ready")
    health["history_paused"] = health.get("state") in {"history_paused_budget", "history_failed_io"}
    settings["health"] = health
    return {"policy": settings, "usage": usage, "status": health}


__all__ = [
    "DEFAULT_TOTAL_BYTES",
    "history_root",
    "load_settings",
    "measure_usage",
    "scope_id",
    "settings_path",
    "snapshot",
    "update_settings",
]
