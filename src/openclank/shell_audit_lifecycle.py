"""Exact, crash-replayable account lifecycle for the shared shell audit log."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from core.atomic_io import atomic_write_bytes, file_fingerprint
from services.memory.skill_lifecycle import locked


class ShellAuditLifecycleError(RuntimeError):
    """The shell audit owner closure is malformed, stale, or unsafe."""


def _owner(value: Any) -> str:
    owner = str(value or "").strip().lower()
    if not owner or "\x00" in owner:
        raise ShellAuditLifecycleError("shell audit owner is required")
    return owner


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _token(row: Mapping[str, Any]) -> str:
    normalized = dict(row)
    normalized["owner"] = "@owner"
    return "sha256:" + hashlib.sha256(_canonical(normalized)).hexdigest()


def _inventory(rows: list[dict[str, Any]], owner: str) -> dict[str, Any]:
    items = sorted(
        _token(row)
        for row in rows
        if str(row.get("owner") or "").strip().lower() == owner
    )
    return {
        "schema_version": 1,
        "count": len(items),
        "fingerprint": "sha256:"
        + hashlib.sha256(_canonical(items)).hexdigest(),
        "items": items,
    }


def _validate_inventory(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != 1:
        raise ShellAuditLifecycleError("shell audit inventory is invalid")
    items = value.get("items")
    if not isinstance(items, list) or any(
        not isinstance(item, str)
        or len(item) != 71
        or not item.startswith("sha256:")
        for item in items
    ):
        raise ShellAuditLifecycleError("shell audit inventory items are invalid")
    canonical_items = sorted(items)
    expected = {
        "schema_version": 1,
        "count": len(canonical_items),
        "fingerprint": "sha256:"
        + hashlib.sha256(_canonical(canonical_items)).hexdigest(),
        "items": canonical_items,
    }
    if (
        value.get("count") != expected["count"]
        or value.get("fingerprint") != expected["fingerprint"]
    ):
        raise ShellAuditLifecycleError("shell audit inventory fingerprint is invalid")
    return expected


class ShellAuditOwnerLifecycle:
    """Move or purge exact-owner JSONL records without exposing their content."""

    def __init__(self, path: str | Path):
        # Keep the final pathname unresolved so `_read` can reject a symlink
        # supplied at construction time.  `Path.resolve()` followed it first
        # and made the safety check observe only the external target.
        self.path = Path(os.path.abspath(os.fspath(path)))

    def _read(self) -> tuple[list[dict[str, Any]], str | None]:
        if self.path.is_symlink():
            raise ShellAuditLifecycleError("shell audit authority may not be a symlink")
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return [], None
        except OSError as exc:
            raise ShellAuditLifecycleError("shell audit authority is unreadable") from exc
        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ShellAuditLifecycleError("shell audit authority is malformed") from exc
            if not isinstance(row, dict):
                raise ShellAuditLifecycleError("shell audit record is not an object")
            rows.append(row)
        return rows, file_fingerprint(str(self.path))

    @staticmethod
    def _output(rows: list[dict[str, Any]]) -> bytes:
        return b"".join(_canonical(row) + b"\n" for row in rows)

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        owner = _owner(owner)
        with locked(str(self.path)):
            rows, _fingerprint = self._read()
            return _inventory(rows, owner)

    def preview_owner_rename(self, source: str, target: str) -> dict[str, Any]:
        source = _owner(source)
        target = _owner(target)
        if source == target:
            raise ShellAuditLifecycleError("shell audit owners must differ")
        with locked(str(self.path)):
            rows, _fingerprint = self._read()
            source_inventory = _inventory(rows, source)
            target_inventory = _inventory(rows, target)
            if target_inventory["count"]:
                raise ShellAuditLifecycleError(
                    "shell audit rename target already contains records"
                )
            return {
                "schema_version": 1,
                "source": source_inventory,
                "target": target_inventory,
            }

    @staticmethod
    def _manifest(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(value, Mapping) or value.get("schema_version") != 1:
            raise ShellAuditLifecycleError("shell audit manifest is invalid")
        source = _validate_inventory(value.get("source"))
        target = _validate_inventory(value.get("target"))
        if target["count"]:
            raise ShellAuditLifecycleError("shell audit target preview is not empty")
        return source, target

    def reconcile_owner_rename(
        self,
        source: str,
        target: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        source = _owner(source)
        target = _owner(target)
        expected, _empty = self._manifest(manifest)
        with locked(str(self.path)):
            rows, before = self._read()
            current_source = _inventory(rows, source)
            current_target = _inventory(rows, target)
            if Counter(current_source["items"]) + Counter(current_target["items"]) != Counter(expected["items"]):
                raise ShellAuditLifecycleError(
                    "shell audit state conflicts with the frozen preview"
                )
            changed = False
            for row in rows:
                if str(row.get("owner") or "").strip().lower() == source:
                    row["owner"] = target
                    changed = True
            if changed:
                atomic_write_bytes(
                    str(self.path),
                    self._output(rows),
                    expected_fingerprint=before,
                )
            after_source = _inventory(rows, source)
            after_target = _inventory(rows, target)
            if after_source["count"] or Counter(after_target["items"]) != Counter(expected["items"]):
                raise ShellAuditLifecycleError("shell audit rename did not converge")
            return {
                "state": "staged",
                "count": expected["count"],
                "source": after_source,
                "target": after_target,
            }

    def compensate_owner_rename(
        self,
        source: str,
        target: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        receipt = self.reconcile_owner_rename(target, source, {
            "schema_version": 1,
            "source": _validate_inventory(manifest.get("source")),
            "target": _validate_inventory(manifest.get("target")),
        })
        return {**receipt, "state": "restored"}

    def purge_owner(
        self,
        owner: str,
        *,
        expected: Mapping[str, Any],
    ) -> dict[str, Any]:
        owner = _owner(owner)
        frozen = (
            _validate_inventory(expected.get("source"))
            if isinstance(expected, Mapping) and "source" in expected
            else _validate_inventory(expected)
        )
        with locked(str(self.path)):
            rows, before_fingerprint = self._read()
            before = _inventory(rows, owner)
            if not Counter(before["items"]) <= Counter(frozen["items"]):
                raise ShellAuditLifecycleError(
                    "shell audit purge conflicts with the frozen preview"
                )
            kept = [
                row
                for row in rows
                if str(row.get("owner") or "").strip().lower() != owner
            ]
            if len(kept) != len(rows):
                atomic_write_bytes(
                    str(self.path),
                    self._output(kept),
                    expected_fingerprint=before_fingerprint,
                )
            after = _inventory(kept, owner)
            return {
                "state": "purged",
                "complete": after["count"] == 0,
                "count": frozen["count"],
                "deleted_now": before["count"],
                "after": after,
            }


def build_shell_audit_owner_lifecycle() -> ShellAuditOwnerLifecycle:
    from src.constants import DATA_DIR

    return ShellAuditOwnerLifecycle(Path(DATA_DIR) / "shell-audit.jsonl")


__all__ = [
    "ShellAuditLifecycleError",
    "ShellAuditOwnerLifecycle",
    "build_shell_audit_owner_lifecycle",
]
