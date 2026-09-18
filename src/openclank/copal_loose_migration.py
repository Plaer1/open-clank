"""No-delete Redb snapshot -> loose-vault staging and verification."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from src.openclank.copal_bridge import CopalBridgeError
from src.openclank.copal_loose import LooseCopalRepository


class LooseMigrationError(ValueError):
    pass


class LooseCopalMigration:
    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.repository = LooseCopalRepository(self.root)

    def dry_run(self, snapshot: dict[str, Any], *, owner: str, workspace: str) -> dict[str, Any]:
        docs = snapshot.get("docs") if isinstance(snapshot, dict) else None
        if not isinstance(docs, list):
            raise LooseMigrationError("snapshot docs must be a list")
        issues: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        for index, document in enumerate(docs):
            if not isinstance(document, dict):
                issues.append({"index": index, "code": "invalid_document"})
                continue
            document_id = str(document.get("id") or "")
            try:
                name = self.repository._safe_name(
                    str(document.get("name") or ""),
                    str(document.get("kind") or "markdown"),
                )
            except CopalBridgeError as error:
                issues.append({"index": index, "id": document_id, "code": "invalid_path", "message": str(error)})
                continue
            if not document_id or document_id in seen_ids:
                issues.append({"index": index, "id": document_id, "code": "duplicate_id"})
            if name in seen_paths:
                issues.append({"index": index, "id": document_id, "code": "path_collision", "path": name})
            seen_ids.add(document_id)
            seen_paths.add(name)
        vault = self.repository._vault(owner, workspace)
        existing = sorted(path.relative_to(vault).as_posix() for path in vault.rglob("*") if path.is_file()) if vault.exists() else []
        if existing:
            issues.append({"code": "target_not_empty", "paths": existing[:100], "count": len(existing)})
        return {"ok": not issues, "owner": owner, "workspace": workspace, "documentCount": len(docs), "issues": issues, "targetVault": str(vault)}

    def stage(self, snapshot: dict[str, Any], *, owner: str, workspace: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
        report = self.dry_run(snapshot, owner=owner, workspace=workspace)
        if not report["ok"]:
            raise LooseMigrationError(json.dumps(report, sort_keys=True))
        vault = Path(report["targetVault"])
        vault.parent.mkdir(parents=True, exist_ok=True)
        staging = vault.parent / f".{vault.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        manifest = {"schemaVersion": 1, "documents": {}, "operations": [], "migration": {"state": "staging", "source": source or {}, "startedAt": time.time()}}
        try:
            for document in snapshot["docs"]:
                document_id = str(document["id"])
                kind = str(document.get("kind") or "markdown")
                name = self.repository._safe_name(str(document["name"]), kind)
                content = str(document.get("text") or "")
                record = {
                    "id": document_id,
                    "owner": owner,
                    "workspace_id": workspace,
                    "kind": kind,
                    "corpus": str(document.get("corpus") or "notes"),
                    "name": name,
                    "path": name,
                    "head": self.repository._fingerprint(content),
                    "createdAt": document.get("createdAt") or time.time(),
                    "updatedAt": document.get("updatedAt") or time.time(),
                    "readOnly": bool(document.get("readOnly")),
                    "trashed": False,
                    "migratedFromHead": document.get("head"),
                }
                manifest["documents"][document_id] = record
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                self.repository._atomic_write(target, content)
                if target.read_text(encoding="utf-8") != content:
                    raise LooseMigrationError(f"staged content verification failed for {name}")
            manifest["migration"]["state"] = "staged"
            self.repository._atomic_write(staging / ".copal" / "manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            descriptor = {"schemaVersion": 1, "state": "staged", "source": source or {}, "owner": owner, "workspace": workspace, "stagingVault": str(staging), "targetVault": str(vault), "documentCount": len(snapshot["docs"])}
            self.repository._atomic_write(staging / ".copal" / "migration-descriptor.json", json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return descriptor
        except BaseException:
            for path in sorted(staging.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
            staging.rmdir()
            raise

    def cutover(self, descriptor: dict[str, Any]) -> dict[str, Any]:
        staging = Path(str(descriptor.get("stagingVault") or "")).resolve()
        target = Path(str(descriptor.get("targetVault") or "")).resolve()
        if not staging.is_dir() or not (staging / ".copal" / "migration-descriptor.json").is_file():
            raise LooseMigrationError("staging vault is missing or unverified")
        if target.exists():
            raise LooseMigrationError("cutover target already exists; refusing overwrite")
        os.replace(staging, target)
        result = dict(descriptor)
        result["state"] = "files-live"
        result["cutoverAt"] = time.time()
        self.repository._atomic_write(target / ".copal" / "migration-descriptor.json", json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return result
