"""Owner-scoped Copal import attachments and export download artifacts.

The native tool boundary deals only in opaque IDs.  This module resolves the
existing upload store, validates the same ZIP safety rules used by the HTTP
route, and persists short-lived export metadata beside the Copal bridge data.
No server path is returned to a model or stored in a tool result.
"""

from __future__ import annotations

import hashlib
import logging
import asyncio
import json
import secrets
import tempfile
import time
import zipfile
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from src.constants import DATA_DIR, UPLOAD_DIR
from src.openclank.copal_planning import event_document_name, event_from_document
from src.upload_handler import UploadHandler, is_valid_upload_id
from src.upload_limits import COPAL_IMPORT_MAX_BYTES


TRANSFER_TTL_SECONDS = 600
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")


class CopalTransferError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_transfer", detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _root(bridge: Any) -> Path:
    root = Path(getattr(bridge, "data_dir", DATA_DIR)).expanduser().resolve() / "copal-transfers"
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for metadata_path in root.glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expired = float(metadata.get("expires") or 0) < now
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if expired:
            for candidate in (metadata_path, metadata_path.with_suffix(".zip")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
    return root


def _token(value: Any, field: str) -> str:
    value = str(value or "").strip()
    if not _TOKEN_RE.fullmatch(value):
        raise CopalTransferError(f"{field} is not a valid opaque transfer ID", code="invalid_id")
    return value


def _upload(attachment_id: Any, owner: str) -> tuple[Path, dict[str, Any]]:
    attachment_id = str(attachment_id or "").strip()
    if not is_valid_upload_id(attachment_id):
        raise CopalTransferError("attachmentId is not a valid uploaded-file ID", code="invalid_attachment")
    handler = UploadHandler(DATA_DIR, UPLOAD_DIR)
    info = handler.get_upload_info(attachment_id)
    if not info:
        raise CopalTransferError("attachment was not found", code="attachment_not_found")
    recorded_owner = info.get("owner")
    if recorded_owner not in (None, "", owner):
        raise CopalTransferError("attachment belongs to another owner", code="attachment_forbidden")
    # Auth-disabled/local uploads may intentionally have no owner metadata;
    # preserve that legacy namespace while still rejecting a recorded foreign
    # owner above. Authenticated rows are resolved with the exact owner.
    resolve_owner = owner if recorded_owner not in (None, "") else None
    resolved_info = handler.resolve_upload(attachment_id, owner=resolve_owner, allow_admin=False)
    if not resolved_info:
        raise CopalTransferError("attachment bytes are unavailable", code="attachment_not_found")
    resolved = Path(str(resolved_info.get("path") or "")).resolve()
    upload_root = Path(UPLOAD_DIR).resolve()
    if upload_root not in resolved.parents or not resolved.is_file():
        raise CopalTransferError("attachment path is outside the upload store", code="attachment_forbidden")
    name = str(info.get("name") or attachment_id)
    if not name.casefold().endswith(".zip"):
        raise CopalTransferError("Copal import requires a ZIP attachment", code="invalid_attachment")
    size = resolved.stat().st_size
    if size > COPAL_IMPORT_MAX_BYTES:
        raise CopalTransferError("attachment exceeds the Copal import limit", code="attachment_too_large")
    return resolved, {"id": attachment_id, "name": name, "size": size, "mime": info.get("mime") or "application/zip"}


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _members(path: Path) -> list[tuple[Any, PurePosixPath]]:
    # Keep ZIP policy identical to the authenticated HTTP importer.
    from routes.copal_routes import _validated_zip_members

    try:
        with zipfile.ZipFile(path) as archive:
            return _validated_zip_members(archive)
    except zipfile.BadZipFile as exc:
        raise CopalTransferError("attachment is not a valid ZIP archive", code="invalid_archive") from exc
    except Exception as exc:
        if isinstance(exc, CopalTransferError):
            raise
        message = str(exc) or "archive failed Copal safety validation"
        raise CopalTransferError(message, code="unsafe_archive") from exc


def preview_import(attachment_id: Any, owner: str, workspace: str, corpus: str) -> dict[str, Any]:
    path, info = _upload(attachment_id, owner)
    members = _members(path)
    names = [relative.as_posix() for _, relative in members]
    return {
        "attachmentId": info["id"],
        "sourceHash": _fingerprint(path),
        "compressedBytes": info["size"],
        "files": len(members),
        "members": names[:200],
        "omittedMembers": max(0, len(names) - 200),
        "corpus": corpus,
        "workspace": workspace,
        "archiveManifest": ".copal/export-manifest.json" in names,
    }


async def apply_import(bridge: Any, *, attachment_id: Any, owner: str, workspace: str, corpus: str) -> dict[str, Any]:
    path, info = _upload(attachment_id, owner)
    with tempfile.TemporaryDirectory(prefix="copal-native-import-") as temporary:
        root = Path(temporary) / "vault"
        root.mkdir()
        members = _members(path)
        expanded = 0
        try:
            with zipfile.ZipFile(path) as archive:
                for info_zip, relative in members:
                    target = root.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    member_bytes = 0
                    with archive.open(info_zip) as source, target.open("xb") as destination:
                        while chunk := source.read(1024 * 1024):
                            member_bytes += len(chunk)
                            expanded += len(chunk)
                            if member_bytes > info_zip.file_size:
                                raise CopalTransferError("archive member size is inconsistent", code="invalid_archive")
                            destination.write(chunk)
                    if member_bytes != info_zip.file_size:
                        raise CopalTransferError("archive member size is inconsistent", code="invalid_archive")
        except zipfile.BadZipFile as exc:
            raise CopalTransferError("attachment is not a valid ZIP archive", code="invalid_archive") from exc

        from routes.copal_routes import _export_restore_identities, _prepare_import_tree

        try:
            restore_ids, restore_manifest = _export_restore_identities(root, members, workspace)
            preserved_paths = {name for name, identity in restore_ids.items() if identity["kind"] == "compatibility"}
            preparation = await asyncio.to_thread(_prepare_import_tree, root, preserved_paths)
        except CopalTransferError:
            raise
        except Exception as exc:
            raise CopalTransferError("archive manifest or preparation failed validation", code="invalid_archive") from exc
        planning = root / ".copal" / "planning.json"
        if not planning.is_file():
            planning = root / "move-data.json"
        result = await bridge.call(
            "import_vault",
            {
                "owner": owner,
                "workspace_id": workspace,
                "path": str(root),
                "planning_path": str(planning) if planning.is_file() else None,
                "note_kind": "wiki" if corpus == "wiki" else "note",
                "restore_ids": restore_ids,
            },
            timeout=120,
        )
        return {
            "imported": result,
            "attachmentId": info["id"],
            "compressedBytes": info["size"],
            "files": len(members),
            "corpus": corpus,
            "preparation": preparation,
            "restoreManifest": {"present": restore_manifest, "identities": len(restore_ids)},
        }


def _export_name(doc: dict[str, Any]) -> str:
    from routes.copal_routes import _safe_export_name

    try:
        return _safe_export_name(doc)
    except Exception as exc:
        raise CopalTransferError("export contains an unsafe document name", code="export_invalid") from exc


def _export_docs(snapshot: dict[str, Any], options: dict[str, Any]) -> list[dict[str, Any]]:
    from routes.copal_routes import _note_view
    docs = [
        _note_view(doc) for doc in snapshot.get("docs") or []
        if isinstance(doc, dict)
        and (not doc.get("readOnly") or doc.get("kind") in {"asset", "compatibility"})
        and doc.get("kind") != "copal-operation"
    ]
    if options.get("includeWiki") is False:
        docs = [doc for doc in docs if doc.get("corpus") != "wiki"]
    if options.get("includeAssets") is False:
        docs = [doc for doc in docs if doc.get("kind") not in {"asset", "compatibility"}]
    return docs


def preview_export(snapshot: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    docs = _export_docs(snapshot, options)
    estimated = 0
    assets = 0
    omissions: list[str] = []
    for doc in docs:
        if doc.get("kind") == "asset":
            assets += 1
            estimated += int(doc.get("size") or 0)
        else:
            estimated += len(str(doc.get("text") or "").encode())
        if doc.get("kind") in {"compatibility", "copal-operation"}:
            omissions.append(str(doc.get("name") or doc.get("id")))
    source_hash = hashlib.sha256(json.dumps([(doc.get("id"), doc.get("head")) for doc in docs], sort_keys=True).encode()).hexdigest()
    return {"documents": len(docs), "assets": assets, "estimatedBytes": estimated, "omissions": omissions[:100], "sourceHash": source_hash, "options": options}


def verify_export_archive(path: Path, *, manifest: dict[str, Any], documents: list[dict[str, Any]], workspace: str) -> dict[str, Any]:
    """Reopen completed bytes and reconcile scope, paths and every payload."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            normalized = [unicodedata.normalize("NFC", name).casefold() for name in names]
            if len(names) != len(set(normalized)):
                raise ValueError("duplicate portable archive paths")
            actual = json.loads(archive.read(".copal/export-manifest.json"))
            if actual != manifest or actual.get("workspace") != workspace:
                raise ValueError("manifest scope changed")
            entries = actual["documents"]
            expected_ids = [(str(doc.get("corpus") or "system"), str(doc.get("id") or "")) for doc in documents]
            actual_ids = [(str(entry["corpus"]), str(entry["id"])) for entry in entries]
            if any(not identity for _, identity in expected_ids) or len(set(expected_ids)) != len(expected_ids) or actual_ids != expected_ids:
                raise ValueError("selected document identities disagree")
            expected_paths = [entry["path"] for entry in entries] + [".copal/export-manifest.json"]
            if names != expected_paths:
                raise ValueError("archive payload set disagrees")
            for entry in entries:
                data = archive.read(entry["path"])
                if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise ValueError("archive payload digest disagrees")
        return {"manifestValid": True, "finished": True, "filenameOnly": False,
                "documents": len(entries), "scope": actual.get("scope") or {}}
    except Exception as exc:
        raise CopalTransferError("completed ZIP failed scope or payload verification", code="export_integrity") from exc


async def create_export(bridge: Any, *, snapshot: dict[str, Any], owner: str, workspace: str, options: dict[str, Any], on_completed=None) -> dict[str, Any]:
    docs = _export_docs(snapshot, options)
    canonical = bool(next((doc for doc in docs if doc.get("kind") == "copal-tracks"), None))
    root = _root(bridge)
    token = secrets.token_urlsafe(32)
    zip_path = root / f"{token}.zip"
    manifest = {"format": "copal-obsidian-export-v1", "exported_at": datetime.now(timezone.utc).isoformat(), "workspace": workspace, "scope": {"includeWiki": options.get("includeWiki") is not False, "includeAssets": options.get("includeAssets") is not False}, "documents": []}
    assets_root = (Path(getattr(bridge, "data_dir", DATA_DIR)) / "assets").resolve()
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for doc in docs:
                export_name = _export_name(doc)
                if event_from_document(doc):
                    export_name = event_document_name(event_from_document(doc))
                if doc.get("corpus") == "wiki":
                    export_name = f".copal/wiki/{export_name}"
                if canonical and doc.get("kind") == "planning":
                    export_name = ".copal/planning.legacy.json"
                if doc.get("kind") in {"asset", "compatibility"}:
                    arguments = {"owner": owner, "workspace_id": workspace, "id": doc.get("id")}
                    if doc.get("corpus") == "wiki":
                        arguments["corpus"] = "wiki"
                    asset = await bridge.call("asset_path", arguments, timeout=60)
                    path = Path(str(asset.get("path") or "")).resolve()
                    vault_for = getattr(bridge, "_vault", None)
                    vault = Path(vault_for(owner, workspace)).resolve() if callable(vault_for) else None
                    if not path.is_file() or not (path.parent == assets_root or (vault is not None and path.is_relative_to(vault))):
                        raise CopalTransferError("export asset bytes are unavailable in this scope", code="export_integrity")
                    content_bytes = path.read_bytes()
                    archive.writestr(export_name, content_bytes)
                else:
                    from routes.copal_routes import _note_markdown
                    content_bytes = (_note_markdown(doc) if doc.get("kind") in {"note", "wiki"} else str(doc.get("text") or "")).encode("utf-8")
                    archive.writestr(export_name, content_bytes)
                manifest["documents"].append({"id": doc.get("id"), "corpus": doc.get("corpus") or "system", "kind": doc.get("kind"), "path": export_name, "size": len(content_bytes), "sha256": hashlib.sha256(content_bytes).hexdigest()})
            archive.writestr(".copal/export-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        verification = verify_export_archive(zip_path, manifest=manifest, documents=docs, workspace=workspace)
        digest = _fingerprint(zip_path)
        expires = time.time() + TRANSFER_TTL_SECONDS
        metadata = {"schemaVersion": 1, "downloadId": token, "owner": owner, "workspace": workspace, "expires": expires, "sha256": digest, "size": zip_path.stat().st_size, "filename": "copal-obsidian-export.zip"}
        (root / f"{token}.json").write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        if on_completed:
            try:
                on_completed(digest, {**verification, "exportId": digest})
            except Exception:
                # The completed export remains usable if achievement delivery
                # fails; do not convert a committed artifact into a retry.
                logging.getLogger(__name__).warning("Export achievement delivery unavailable")
        return {"verification": verification, "downloadId": token, "downloadUrl": f"/api/copal/export/download/{token}", "expiresAt": expires, "size": metadata["size"], "sha256": digest, "workspace": workspace}
    except Exception:
        for candidate in (zip_path, root / f"{token}.json"):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        raise


def load_download(bridge: Any, *, download_id: Any, owner: str, workspace: str) -> tuple[Path, dict[str, Any]]:
    token = _token(download_id, "downloadId")
    root = _root(bridge)
    metadata_path = root / f"{token}.json"
    zip_path = root / f"{token}.zip"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CopalTransferError("download artifact was not found", code="download_not_found") from exc
    if metadata.get("owner") != owner or metadata.get("workspace") != workspace:
        raise CopalTransferError("download artifact is not available to this owner/workspace", code="download_forbidden")
    if float(metadata.get("expires") or 0) < time.time():
        for candidate in (metadata_path, zip_path):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        raise CopalTransferError("download artifact has expired", code="download_expired")
    if not zip_path.is_file() or _fingerprint(zip_path) != metadata.get("sha256"):
        raise CopalTransferError("download artifact failed integrity validation", code="download_integrity")
    return zip_path, metadata
