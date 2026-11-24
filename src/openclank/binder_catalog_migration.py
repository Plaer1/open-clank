"""Read-only building blocks for a separately authorized Binder conversion.

The caller must supply a verified account/owner and an explicitly selected vault.
This module has no CLI, runtime startup, preference write, or apply operation.
Its output is a review draft, not authority to modify a live catalog.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from src.openclank.binder_catalog import empty_catalog, prepare_update, validate_catalog


class PreflightError(ValueError):
    pass


def _digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def inventory_vault(vault: Path, *, expected_owner: str) -> dict[str, Any]:
    """Inventory one caller-selected vault without invoking repository startup.

    Refuse symlinks and changing/unreadable files. No file contents are returned.
    This is observational, not a filesystem lock or proof of authenticated identity.
    """
    vault = Path(vault)
    if not expected_owner or vault.is_symlink() or not vault.is_dir():
        raise PreflightError("The selected vault or owner is unavailable")
    entries: list[list[Any]] = []
    references: list[tuple[str, str, str]] = []
    unsupported = 0
    total_bytes = 0
    manifest: dict[str, Any] | None = None
    for directory, dirs, files in os.walk(vault, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
        base = Path(directory)
        for name in sorted(dirs + files):
            path = base / name
            before = path.lstat()
            relative = path.relative_to(vault).as_posix()
            if len(entries) >= 20000:
                raise PreflightError("The selected vault exceeds the inventory entry limit")
            if stat.S_ISLNK(before.st_mode):
                raise PreflightError("The selected vault contains a symlink")
            if stat.S_ISDIR(before.st_mode):
                entries.append(["directory", relative])
                continue
            if not stat.S_ISREG(before.st_mode):
                raise PreflightError("The selected vault contains a special file")
            total_bytes += before.st_size
            if total_bytes > 512 * 1024 * 1024 or relative == ".copal/manifest.json" and before.st_size > 16 * 1024 * 1024:
                raise PreflightError("The selected vault exceeds the inventory byte limit")
            digest = hashlib.sha256()
            scan_text = relative.lower().endswith((".md", ".markdown")) and not relative.startswith(".copal/")
            if scan_text and before.st_size > 8 * 1024 * 1024:
                raise PreflightError("A document exceeds the bounded reference scan")
            chunks = [] if relative == ".copal/manifest.json" or scan_text else None
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as handle:
                while block := handle.read(1024 * 1024):
                    digest.update(block)
                    if chunks is not None:
                        chunks.append(block)
            after = path.lstat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise PreflightError("The selected vault changed during inventory")
            entries.append(["file", relative, before.st_size, digest.hexdigest()])
            if chunks is not None and relative == ".copal/manifest.json":
                try:
                    manifest = json.loads(b"".join(chunks))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise PreflightError("The selected manifest is unreadable") from exc
            elif chunks is not None:
                try:
                    text = b"".join(chunks).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise PreflightError("A document cannot be scanned as UTF-8") from exc
                unsupported += len(re.findall(r"(?m)^\s*\[[^\]]+\]:\s*\S+|<\s*(?:a|img|video|audio|source)\b", text, re.IGNORECASE))
                for match in re.finditer(r"!?\[\[([^\]\n]+)\]\]|!?\[[^\]\n]*\]\(([^)\n]+)\)", text):
                    references.append((relative, "wiki" if match.group(1) is not None else "markdown", match.group(1) or match.group(2)))
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("documents"), dict):
        raise PreflightError("The selected manifest is unavailable")
    records = []
    file_paths = {entry[1] for entry in entries if entry[0] == "file"}
    missing = []
    for document_id, record in sorted(manifest["documents"].items()):
        if not isinstance(record, dict) or record.get("owner") != expected_owner:
            raise PreflightError("The manifest contains another or invalid owner")
        raw_path = record.get("trashPath") if record.get("trashed") else record.get("path")
        if raw_path:
            path = PurePosixPath(str(raw_path))
            if path.is_absolute() or ".." in path.parts or "\\" in str(raw_path):
                raise PreflightError("The manifest contains an invalid path")
            if path.as_posix() not in file_paths:
                missing.append(str(document_id))
        records.append({"id":str(document_id), "kind":record.get("kind"), "name":record.get("name"),
                        "path":raw_path, "head":record.get("head"), "trashed":bool(record.get("trashed"))})
    entries.sort(key=lambda entry: entry[1])
    by_name: dict[str, list[str]] = {}
    for record in records:
        if not record["trashed"] and isinstance(record["name"], str):
            name = record["name"].casefold()
            by_name.setdefault(name, []).append(record["id"])
            base = PurePosixPath(name).name
            if base != name:
                by_name.setdefault(base, []).append(record["id"])
    unresolved = []
    for origin, kind, raw in references:
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if not target:
            continue
        if kind == "wiki":
            candidates = by_name.get(target.casefold(), []) or by_name.get((target + ".md").casefold(), [])
            resolved = len(set(candidates)) == 1
        else:
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue
            relative = PurePosixPath(origin).parent / unquote(parsed.path)
            stack: list[str] = []
            for part in relative.parts:
                if part == "..":
                    if not stack:
                        break
                    stack.pop()
                elif part != ".":
                    stack.append(part)
            else:
                if "/".join(stack) in file_paths:
                    continue
            resolved = False
        if not resolved:
            unresolved.append(hashlib.sha256((origin + "\0" + kind + "\0" + raw).encode()).hexdigest())
    return {"fingerprint":_digest(entries), "entries":entries, "records":records, "missingIds":missing,
            "references":{"count":len(references), "unresolvedDigests":unresolved,
                          "unsupportedCount":unsupported, "coverage":"literal Markdown links and wiki links only"},
            "linksResolved":not unresolved and not unsupported, "complete":not missing and not unresolved and not unsupported}


def propose_catalog(*, account_id: str, catalog: dict[str, Any] | None,
                    binder_id: str, display_name: str, source: dict[str, Any],
                    expected_revision: int) -> dict[str, Any]:
    """Build an account-scoped CAS proposal without committing it."""
    current = empty_catalog(account_id) if catalog is None else validate_catalog(catalog, account_id)
    proposal = json.loads(json.dumps(current))
    existing = proposal["descriptors"].get(binder_id)
    if existing and existing["source"] != source:
        raise PreflightError("The selected Binder ID already names another source")
    proposal["descriptors"][binder_id] = {"id":binder_id, "displayName":display_name,
        "provider":"copal", "source":source, "enabled":True,
        "revision":existing["revision"] if existing else 0}
    proposal["configuredIds"] = [binder_id]
    proposal["defaultId"] = binder_id
    after = prepare_update(current, proposal, account_id, expected_revision)
    return {"beforeDigest":_digest(current), "expectedRevision":expected_revision,
            "request":proposal, "afterDigest":_digest(after),
            "applyReady":False, "reason":"Authenticated source proof, link invariants and commit receipt are not joined"}
