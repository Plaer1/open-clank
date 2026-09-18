"""Small durable guarded-commit coordinator for loose Copal storage.

The coordinator is called only while ``copal_commit_lock`` is held.  It
supports many exact read guards and one target write; broader multi-write
atomicity belongs to a coordinator with a stronger transaction primitive.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from src.openclank.copal_bridge import CopalBridgeError


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class GuardedCommitCoordinator:
    VERSION = 1

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / ".copal-guarded-journal.json"

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schemaVersion": self.VERSION, "pending": {}, "receipts": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CopalBridgeError("guarded commit journal is unreadable") from exc
        if not isinstance(value, dict) or value.get("schemaVersion") != self.VERSION:
            raise CopalBridgeError("guarded commit journal schema is unsupported")
        # Read the pre-compaction shape once and rewrite it on the next sync.
        if isinstance(value.get("actions"), dict):
            pending = {key: entry for key, entry in value["actions"].items() if isinstance(entry, dict) and entry.get("status") == "pending"}
            receipts = {key: entry.get("receipt", entry) for key, entry in value["actions"].items() if isinstance(entry, dict) and entry.get("status") == "complete"}
            return {"schemaVersion": self.VERSION, "pending": pending, "receipts": receipts}
        if not isinstance(value.get("pending"), dict) or not isinstance(value.get("receipts"), dict):
            raise CopalBridgeError("guarded commit journal schema is unsupported")
        return value
        return value

    def _write(self, value: dict[str, Any]) -> None:
        from src.openclank.copal_loose import LooseCopalRepository
        receipts = dict(value.get("receipts") or {})
        if len(receipts) > 1000:
            receipts = dict(list(receipts.items())[-1000:])
        value = {"schemaVersion": self.VERSION, "pending": dict(value.get("pending") or {}), "receipts": receipts}
        LooseCopalRepository._atomic_write(self.path, _canonical(value))

    @staticmethod
    def _action_id(args: dict[str, Any]) -> str:
        action_id = str(args.get("action_id") or "").strip()
        if not action_id:
            raise CopalBridgeError("action_id is required")
        return action_id

    @staticmethod
    def _actor_id(args: dict[str, Any]) -> str:
        actor_id = str(args.get("actor_id") or "").strip()
        if not actor_id:
            raise CopalBridgeError("actor_id is required")
        return actor_id

    def _request_digest(self, args: dict[str, Any]) -> str:
        supplied = str(args.get("request_digest") or "").strip()
        material = {key: value for key, value in args.items() if key not in {"request_digest"}}
        expected = _digest(material)
        if supplied and supplied != expected:
            raise CopalBridgeError("request digest does not match payload")
        return supplied or expected

    @staticmethod
    def _required_revision(value: Any, *, label: str) -> dict[str, str]:
        # `head` is the one compatibility alias; all internal receipts use
        # the tagged copalHead form so callers cannot omit a guard silently.
        if isinstance(value, dict) and "kind" not in value:
            raise CopalBridgeError(f"{label} must be a tagged copalHead revision")
        if isinstance(value, dict) and "kind" in value:
            kind, revision = str(value.get("kind") or ""), str(value.get("value") or "")
            if kind != "copalHead" or not revision:
                raise CopalBridgeError(f"{label} must be a non-empty copalHead revision")
            return {"kind":"copalHead", "value":revision}
        revision = str(value or "")
        if not revision:
            raise CopalBridgeError(f"{label} revision is required")
        return {"kind":"copalHead", "value":revision}

    @staticmethod
    def _guard_revision(repo: Any, guard: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        owner = str(guard.get("owner") or "")
        workspace = str(guard.get("workspace_id") or guard.get("workspace") or "")
        document_id = str(guard.get("id") or guard.get("document_id") or "")
        vault, manifest = repo._scope({"owner": owner, "workspace_id": workspace})
        record = repo._doc(manifest, document_id)
        current = repo._record_doc(vault, record, include_body=True)
        expected = GuardedCommitCoordinator._required_revision(guard.get("revision", guard.get("head")), label="guard")
        expected_value = expected["value"]
        actual = current.get("head")
        if expected_value is not None and str(expected_value) != str(actual):
            raise _GuardConflict(owner, workspace, document_id, str(expected_value), str(actual))
        return vault, record

    def reconcile(self, repo: Any) -> None:
        journal = self._read()
        changed = False
        blocked = False
        for action_id, entry in list(journal["pending"].items()):
            target = entry.get("target") or {}
            try:
                vault, manifest = repo._scope({"owner":target.get("owner"), "workspace_id":target.get("workspace_id")})
                record = repo._doc(manifest, str(target.get("id") or ""))
                current = repo._record_doc(vault, record, include_body=True)
                desired = str(target.get("content") or "")
                desired_head = repo._fingerprint(desired)
                if current.get("head") == desired_head:
                    record["head"] = desired_head
                    record["updatedAt"] = time.time()
                    operations = manifest.get("operations") or []
                    if not any(item.get("actionId") == action_id for item in operations if isinstance(item, dict)):
                        repo._operation(manifest, "guarded_write", f"guarded_write {record.get('name', record.get('id'))}", document_id=record["id"], action_id=action_id)
                    repo._save(vault, manifest)
                    journal["receipts"][action_id] = self._receipt(entry, {"kind":"copalHead", "value":desired_head}, reconciled=True)
                    journal["pending"].pop(action_id, None)
                elif current.get("head") == target.get("before_head"):
                    repo._write_record(vault, manifest, record, desired, operation="guarded_write", action_id=action_id)
                    journal["receipts"][action_id] = self._receipt(entry, {"kind":"copalHead", "value":desired_head}, reconciled=True)
                    journal["pending"].pop(action_id, None)
                else:
                    journal["receipts"][action_id] = {"outcome":"partial", "action_id":action_id, "actor_id":entry.get("actor_id"), "request_digest":entry.get("request_digest"), "reason":"target changed externally", "resource":self._resource(target)}
                    journal["pending"].pop(action_id, None)
                changed = True
            except Exception as exc:
                # Keep transient IO/schema failures pending for a fresh-process
                # retry. Only a confirmed external target fingerprint is
                # terminal partial state.
                if isinstance(exc, _GuardConflict):
                    journal["receipts"][action_id] = {"outcome":"partial", "action_id":action_id, "actor_id":entry.get("actor_id"), "request_digest":entry.get("request_digest"), "reason":str(exc), "resource":self._resource(target)}
                    journal["pending"].pop(action_id, None)
                    changed = True
                else:
                    blocked = True
        if changed:
            self._write(journal)
        if blocked:
            raise CopalBridgeError("guarded commit recovery is pending")

    @staticmethod
    def _resource(target: dict[str, Any]) -> dict[str, Any]:
        return {"owner":target.get("owner"), "workspace_id":target.get("workspace_id"), "id":target.get("id")}

    def _receipt(self, entry: dict[str, Any], revision: dict[str, str], *, reconciled: bool = False) -> dict[str, Any]:
        target = entry.get("target") or {}
        return {"outcome":"applied", "action_id":entry.get("action_id"), "actor_id":entry.get("actor_id"), "request_digest":entry.get("request_digest"), "before":{"kind":"copalHead", "value":target.get("before_head")}, "after":revision, "revision":revision, "resource":self._resource(target), **({"reconciled":True} if reconciled else {})}

    def execute(self, repo: Any, args: dict[str, Any]) -> dict[str, Any]:
        journal = self._read()
        action_id = self._action_id(args)
        actor_id = self._actor_id(args)
        digest = self._request_digest(args)
        existing = journal["receipts"].get(action_id) or journal["pending"].get(action_id)
        if existing:
            if existing.get("actor_id") != actor_id or existing.get("request_digest") != digest:
                return {"outcome":"idempotency_conflict", "action_id":action_id}
            if action_id in journal["receipts"]:
                return dict(existing)

        guards = args.get("guards") or []
        operations = args.get("operations") or []
        if not isinstance(guards, list) or not isinstance(operations, list) or len(operations) != 1:
            return {"outcome":"unsupported", "reason":"commit_guarded currently supports exactly one target write"}
        target = operations[0]
        if not isinstance(target, dict):
            raise CopalBridgeError("target operation is invalid")
        if target.get("kind") != "write":
            return {"outcome":"unsupported", "reason":"only write target operations are supported"}
        for guard in guards:
            if not isinstance(guard, dict):
                raise CopalBridgeError("guard is invalid")
            try:
                self._guard_revision(repo, guard)
            except _GuardConflict as conflict:
                return conflict.receipt(action_id)

        try:
            vault, manifest = repo._scope({"owner":target.get("owner"), "workspace_id":target.get("workspace_id")})
            record = repo._doc(manifest, str(target.get("id") or target.get("document_id") or ""))
            current = repo._record_doc(vault, record, include_body=True)
        except Exception as exc:
            raise CopalBridgeError("guarded target is unavailable") from exc
        expected = self._required_revision(target.get("revision", target.get("head", target.get("expected_head"))), label="target")
        expected_value = expected["value"]
        if str(expected_value) != str(current.get("head")):
            return _GuardConflict(str(target.get("owner")), str(target.get("workspace_id")), str(record["id"]), str(expected_value), str(current.get("head"))).receipt(action_id)
        if record.get("readOnly"):
            return {"outcome":"forbidden", "action_id":action_id, "reason":"target document is read-only"}
        content = str(target.get("content") or "")
        entry = {
            "action_id":action_id, "actor_id":actor_id, "request_digest":digest,
            "before":{"kind":"copalHead", "value":current.get("head")},
            "target": {"owner":target.get("owner"), "workspace_id":target.get("workspace_id"), "id":record["id"], "content":content, "before_head":current.get("head")},
            "created_at":time.time(),
        }
        journal["pending"][action_id] = entry
        self._write(journal)
        try:
            result = repo._write_record(vault, manifest, record, content, operation="guarded_write", action_id=action_id)
            revision_kind = str((target.get("revision") or {}).get("kind") or "copalHead") if isinstance(target.get("revision"), dict) else "copalHead"
            revision = {"kind":revision_kind, "value":str(result.get("head") or repo._fingerprint(content))}
            receipt = self._receipt(entry, revision)
            journal["receipts"][action_id] = receipt
            journal["pending"].pop(action_id, None)
            try:
                self._write(journal)
            except Exception as exc:
                # The target write is real, but durable receipt publication is
                # not. Leave the pending decision recoverable on disk when
                # possible and report the honest partial outcome.
                journal["pending"][action_id] = entry
                try:
                    self._write(journal)
                except Exception:
                    pass
                return {"outcome":"partial", "action_id":action_id, "reason":"journal sync failed"}
            return receipt
        except Exception as exc:
            # Keep the write-ahead intent for reconciliation; a transient
            # failure must not erase the only recovery record.
            self._write(journal)
            return {"outcome":"partial", "action_id":action_id, "reason":str(exc)}


class _GuardConflict(Exception):
    def __init__(self, owner: str, workspace: str, document_id: str, expected: str, actual: str):
        self.owner, self.workspace, self.document_id, self.expected, self.actual = owner, workspace, document_id, expected, actual

    def receipt(self, action_id: str) -> dict[str, Any]:
        return {"outcome":"conflict", "action_id":action_id, "resource":{"owner":self.owner, "workspace_id":self.workspace, "id":self.document_id}, "expected":self.expected, "actual":self.actual}


__all__ = ["GuardedCommitCoordinator"]
