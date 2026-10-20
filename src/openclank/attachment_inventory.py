"""Typed attachment inventory of existing authorities; never a content store.

Payload owners enumerate retained content. Only supported resource links and
structured attachment fields identify bytes: an arbitrary hex string is not a
reference. Inventory errors are explicit and cannot authorize retirement.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

CONTRACT = "openclank.attachment-reference-inventory/v1"
UPLOAD = re.compile(r"[a-fA-F0-9]{32}(?:\.[A-Za-z0-9]+)?\Z")
UPLOAD_LINK = re.compile(r"(?:odysseus://attachment/|/api/upload/|upload:)([a-fA-F0-9]{32}(?:\.[A-Za-z0-9]+)?)(?=[^A-Za-z0-9_.]|$)")
PDF_LINK = re.compile(r"<!--\s*pdf(?:_form)?_source\b[^>]*\bupload_id=[\"']([a-fA-F0-9]{32}(?:\.[A-Za-z0-9]+)?)[\"'][^>]*-->", re.I)
ATTACHMENT_LINE = re.compile(r"\[Attachment:[^\]\r\n]*\|\s*id=([a-fA-F0-9]{32}(?:\.[A-Za-z0-9]+)?)(?=\s*\||\s*\])")
IMAGE_ID = r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
IMAGE_LINK = re.compile(r"image:(" + IMAGE_ID + r")(?=[^A-Za-z0-9_-]|$)")
RESOURCE_LINK = re.compile(r"(?:resource_ref|file_ref|ref)=(rr1\.[A-Za-z0-9_%=-]+)")


def _typed_resource_token(token: str) -> set[tuple[str, str]]:
    from urllib.parse import unquote
    from src.openclank.resource_refs import _decode_resource_ref
    from src.openclank.resource_refs import ResourceRefError
    try:
        reference = _decode_resource_ref(unquote(token))
    except ResourceRefError:
        # A malformed example token does not name an owner-issued resource.
        # Writers continue to accept arbitrary authored prose/code.
        return set()
    if reference.provider == "files" and reference.origin_id.startswith("image:"):
        return {("image", reference.origin_id.removeprefix("image:"))}
    if reference.origin_id.startswith("upload:"):
        return {("upload", reference.origin_id.removeprefix("upload:"))}
    return set()


def resource_references(value: Any) -> set[tuple[str, str]]:
    """Parse supported persisted links and typed structured references."""
    result: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        for key in ("resource_ref", "file_ref", "ref"):
            token = value.get(key)
            if isinstance(token, str) and token.startswith("rr1."):
                result.update(_typed_resource_token(token))
        if value.get("type") == "attachment_ref":
            identifier = str(value.get("attachment_id") or "")
            if UPLOAD.fullmatch(identifier):
                result.add(("upload", identifier))
        # Message attachment metadata is the existing attachment contract.
        for item in value.get("attachments", []) if isinstance(value.get("attachments"), list) else []:
            if isinstance(item, dict):
                identifier = str(item.get("id") or item.get("attachment_id") or "")
                if UPLOAD.fullmatch(identifier):
                    result.add(("upload", identifier))
        for event in value.get("tool_events", []) if isinstance(value.get("tool_events"), list) else []:
            if isinstance(event, dict) and event.get("image_id"):
                result.add(("image", str(event["image_id"]).removeprefix("image:")))
        for nested in value.values():
            result.update(resource_references(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            result.update(resource_references(nested))
    elif isinstance(value, str):
        result.update(("upload", identifier) for identifier in UPLOAD_LINK.findall(value))
        result.update(("upload", identifier) for identifier in PDF_LINK.findall(value))
        result.update(("upload", identifier) for identifier in ATTACHMENT_LINE.findall(value))
        result.update(("image", identifier) for identifier in IMAGE_LINK.findall(value))
        for token in RESOURCE_LINK.findall(value):
            result.update(_typed_resource_token(token))
        if value.lstrip().startswith(("{", "[")):
            try:
                result.update(resource_references(json.loads(value)))
            except (ValueError, TypeError):
                pass
    return result


class DomainInventory:
    def __init__(self, domain: str):
        self.domain = domain
        self.rows: list[dict[str, str]] = []
        self.material: list[tuple[str, str]] = []
        self.errors: list[str] = []

    def payload(self, identity: str, value: Any) -> None:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.material.append((identity, hashlib.sha256(encoded.encode()).hexdigest()))
        self.rows.extend({"source": identity, "provider": provider, "resource_id": identifier}
                         for provider, identifier in sorted(resource_references(value)))

    def wire(self) -> dict[str, Any]:
        return {"domain": self.domain, "complete": not self.errors,
                "generation": hashlib.sha256(json.dumps(sorted(self.material)).encode()).hexdigest(),
                "payload_count": len(self.material), "references": self.rows, "errors": self.errors}


def loose_copal_inventory(repository) -> dict[str, Any]:
    """Read the actual loose manifest, current bytes and every retained snapshot."""
    from src.openclank.copal_commit_lock import copal_commit_lock
    inventory = DomainInventory("copal-loose")
    with copal_commit_lock(repository.data_dir):
        for manifest_path in sorted(repository.data_dir.glob("*/*/.copal/manifest.json")):
            try:
                manifest = repository._read_json_file(manifest_path, label="attachment inventory manifest")
                if manifest.get("schemaVersion") != repository.VERSION or not isinstance(manifest.get("documents"), dict):
                    raise ValueError("unsupported loose manifest")
                vault = manifest_path.parent.parent
                for identifier, record in sorted(manifest["documents"].items()):
                    if not isinstance(record, dict):
                        raise ValueError("invalid document record")
                    source = f"{record.get('owner')}/{record.get('workspace_id')}/{identifier}"
                    inventory.payload(source + "/metadata", record)
                    if repository._is_official_reference(record) and not record.get("trashed"):
                        resolved = repository._record_doc(vault, record, include_body=True)
                        if not isinstance(resolved.get("text"), str):
                            raise ValueError("installed official reference body is unavailable")
                        inventory.payload(source + "/installed-official", resolved)
                    elif record.get("kind") not in {"asset", "compatibility"}:
                        path = vault / str((record.get("trashPath") if record.get("trashed") else record.get("path")) or "")
                        if path.is_symlink() or not path.resolve().is_relative_to(vault.resolve()):
                            raise ValueError("invalid current document path")
                        if path.is_file():
                            inventory.payload(source + "/head", path.read_text(encoding="utf-8"))
                        else:
                            raise ValueError("missing current document")
                    for snapshot in sorted(repository._history_dir(vault, record).glob("*.json")):
                        if snapshot.is_symlink():
                            raise ValueError("invalid retained snapshot path")
                        value = json.loads(snapshot.read_text(encoding="utf-8"))
                        if not isinstance(value, dict) or not isinstance(value.get("content"), str):
                            raise ValueError("unreadable retained snapshot")
                        inventory.payload(source + "/" + snapshot.stem, value)
            except (OSError, ValueError, TypeError, RuntimeError) as error:
                inventory.errors.append(f"{manifest_path}: {error}")
    return inventory.wire()


def skills_inventory(manager) -> dict[str, Any]:
    """Enumerate all authored files, immutable bundle files and old snapshots.

    Walk retained storage, including skills with no current published pointer;
    current-head discovery would miss retired/draft bundle references.
    """
    inventory = DomainInventory("skills")
    root = Path(manager.skills_root)
    paths = sorted(root.rglob("*")) if root.exists() else []
    if Path(manager.legacy_file).exists():
        paths.append(Path(manager.legacy_file))
    for path in paths:
        try:
            if path.is_symlink():
                raise ValueError("symlink in retained skill storage")
            if not path.is_file() or path.name.endswith(".lock"):
                continue
            # Bundles allow binary assets. Their bytes are independent; only
            # UTF-8 authored content can contain a supported external link.
            content = path.read_bytes()
            inventory.material.append((str(path), hashlib.sha256(content).hexdigest()))
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            inventory.payload(str(path), text)
        except (OSError, ValueError) as error:
            inventory.errors.append(f"{path}: {error}")
    return inventory.wire()


# Canonical retained Memery columns, never current-head joins. These include
# evidence hidden by later revisions, document chunks and source provenance.
MEMERY_PAYLOADS = {
    "curated": ("content", "source", "metadata", "source_message_ids"),
    "raw": ("content", "metadata"),
    "facts": ("content", "entities"),
    "candidates": ("content", "source", "source_uri", "source_message_ids"),
    "memory_quarantine": ("content", "payload"),
    "memory_tombstones": ("recovery_payload",),
    "memories": ("text", "source_uri", "source_message_ids"),
    "fm_v2_entity_revisions": ("payload",),
    "fm_v2_knowledge_revisions": ("value_json", "evidence_json"),
    "fm_v2_candidate_revisions": ("proposal_json",),
    "fm_v2_sources": ("source_uri",),
    "fm_v2_evidence": ("locator_json", "quote", "raw_value_json", "normalized_value_json"),
    "fm_v2_chunks": ("text", "locator_json"),
    "fm_v2_media_representations": ("text", "provenance_json"),
    "fm_v2_outbox": ("payload_json",),
}


def memery_inventory(db_path: str | Path) -> dict[str, Any]:
    inventory = DomainInventory("memery")
    path = Path(db_path).resolve()
    legacy = path.parent / "memory.json"
    if legacy.exists():
        try:
            inventory.payload("memory.json", json.loads(legacy.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            inventory.errors.append(f"memory.json: {error}")
    if not path.exists():
        return inventory.wire()
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("BEGIN")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"fm_v2_sources", "fm_v2_evidence", "fm_v2_entity_revisions", "fm_v2_knowledge_revisions"}
            if not required.issubset(tables):
                raise ValueError("legacy Memery store requires explicit current-schema conversion")
            for table, expected in MEMERY_PAYLOADS.items():
                if table not in tables:
                    continue
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                payload_columns = [column for column in expected if column in columns]
                if not payload_columns:
                    raise ValueError(f"unsupported retained payload schema: {table}")
                selected = ",".join('"' + column + '"' for column in columns)
                for ordinal, row in enumerate(connection.execute(f'SELECT {selected} FROM "{table}"')):
                    record = dict(zip(columns, row))
                    identity = "/".join(str(record.get(key) or "") for key in ("owner_id", "source_id", "entity_id", "block_id", "revision", "evidence_id"))
                    inventory.payload(f"{table}/{identity}/{ordinal}", {key: record[key] for key in payload_columns})
    except (sqlite3.Error, OSError, ValueError) as error:
        inventory.errors.append(str(error))
    return inventory.wire()


def native_copal_inventory(payloads: dict[str, Any]) -> dict[str, Any]:
    inventory = DomainInventory("copal-native")
    if payloads.get("contract") != "openclank.attachment-reference-payloads/v1" or payloads.get("complete") is not True:
        inventory.errors.append("native retained reference operation is unavailable")
    else:
        for row in payloads.get("rows", []):
            inventory.payload("/".join(str(row.get(key) or "") for key in ("owner", "workspace_id", "document_id", "commit")), row)
    return inventory.wire()


def lore_inventory(payloads: dict[str, Any]) -> dict[str, Any]:
    inventory = DomainInventory("lore")
    if payloads.get("contract") != "openclank.history-reference-payloads/v1" or payloads.get("complete") is not True:
        inventory.errors.extend(payloads.get("errors") or ["retained Lore payload coverage is unavailable"])
    for row in payloads.get("rows", []):
        inventory.payload(str(row.get("source") or ""), row.get("payload"))
    return inventory.wire()


def initialize_known_empty_owner(handler) -> bool:
    """Create only a proven new-install empty baseline; never inspect legacy.

    The core initializer supplies its positive fresh-store fact. Native stores
    initialized earlier in startup must prove current schema and zero retained
    payloads; a failed read or historical provider root cannot mean empty.
    """
    import os
    from src.openclank.conversation_archive import ArchiveUnavailableError
    from core.database import CORE_CREATED_FRESH
    from src.constants import DATA_DIR, FM_DB_PATH, MEMORY_FILE
    from src.openclank.history_paths import history_root, settings_path
    if not CORE_CREATED_FRESH or (Path(handler.upload_dir) / "uploads.json").exists():
        return False
    marker = Path(handler.upload_dir) / ".attachment-inventory.json"
    if marker.exists():
        return False
    try:
        from src.openclank.conversation_archive import default_db_path
        if Path(default_db_path()).exists():
            return False
        copal_root = Path(os.environ.get("COPAL_LOOSE_ROOT") or Path(DATA_DIR) / "copal-vaults")
        if copal_root.exists() and any(copal_root.iterdir()):
            return False
        if history_root().exists() or (settings_path().parent / "history.redb").exists():
            return False
        skills_root = Path(DATA_DIR) / "skills"
        if skills_root.exists() and any(path.is_file() or path.is_symlink() for path in skills_root.rglob("*")):
            return False
        if (Path(DATA_DIR) / "skills.json").exists():
            return False
        if Path(MEMORY_FILE).exists() and json.loads(Path(MEMORY_FILE).read_text()) != []:
            return False
        memery = memery_inventory(FM_DB_PATH)
        if not memery["complete"] or memery["payload_count"]:
            return False
        domains = [DomainInventory(domain).wire() for domain in ("copal", "skills", "memery", "lore")]
        handler._atomic_write_json(str(marker), {"contract": CONTRACT, "complete": True, "origin": "new-empty-owner", "domains": domains, "errors": []})
        return True
    except (OSError, ValueError, RuntimeError, ArchiveUnavailableError):
        return False
