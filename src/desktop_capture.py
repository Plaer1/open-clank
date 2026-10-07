"""Task-triggered native desktop capture with owner-scoped admission.

The capture runner is injectable so policy and argv behavior can be tested
without spawning ``screencapture`` or taking pixels.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import struct
import hashlib
import tempfile
import signal
import sys
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from src.settings import get_user_setting

# Host staging advertises a 64 MiB total ceiling. Keep capture admission at
# that provider-safe bound; larger source limits belong to other media paths.
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
CAPTURE_TIMEOUT_SECONDS = 15
CAPTURE_PREF = "desktop_capture_enabled"
_NATIVE_CACHE = Path(os.environ.get("OPEN_CLANK_DATA_DIR", "/tmp/openclank-data")) / "cache" / "native" / "desktop-capture"


class _NativeDesktopHelpers:
    """Build and invoke the signed-source helpers only on a macOS host."""
    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def _executable(self, name: str, source: Path) -> Path:
        if sys.platform != "darwin":
            raise DesktopCaptureError("native desktop helper is unavailable", code="ocr_unavailable")
        if getattr(sys, "frozen", False):
            from src.runtime_paths import get_app_root

            executable = Path(get_app_root()) / "bin" / name
            if executable.is_file() and os.access(executable, os.X_OK):
                return executable
            raise DesktopCaptureError("bundled native desktop helper is unavailable", code="ocr_unavailable")
        import stat
        source_bytes = source.read_bytes()
        digest = hashlib.sha256(source_bytes).hexdigest()[:24]
        cache = _NATIVE_CACHE.resolve()
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        if cache.stat().st_uid != os.getuid() or cache.stat().st_mode & 0o777 != 0o700:
            raise DesktopCaptureError("native desktop helper cache is unavailable", code="ocr_unavailable")
        target = cache / f"{name}-{digest}"
        try:
            info = target.lstat()
            if info.st_uid == os.getuid() and stat.S_ISREG(info.st_mode) and info.st_mode & 0o777 == 0o700:
                return target
        except FileNotFoundError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=f".{name}-", dir=cache)
        os.close(fd)
        temporary_path = Path(temporary)
        try:
            result = subprocess.run(["/usr/bin/swiftc", str(source), "-O", "-o", str(temporary_path)], capture_output=True, text=True, timeout=20)
            if result.returncode != 0:
                raise DesktopCaptureError("native desktop helper failed to compile", code="ocr_unavailable")
            os.chmod(temporary_path, 0o700)
            try:
                os.link(temporary_path, target)
            except FileExistsError:
                pass
            return target
        except subprocess.TimeoutExpired as exc:
            raise DesktopCaptureError("native desktop helper compilation timed out", code="ocr_unavailable") from exc
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    def metadata(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        executable = self._executable("desktop-metadata", self.root / "native" / "DesktopMetadata.swift")
        try:
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=5)
            value = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise DesktopCaptureError("desktop metadata is unavailable", code="target_unavailable") from exc
        if not isinstance(value, Mapping):
            raise DesktopCaptureError("desktop metadata is invalid", code="target_unavailable")
        target = str(request.get("target") or "display")
        if target == "region":
            region = request.get("region")
            displays = value.get("displays") or []
            valid = isinstance(region, (list, tuple)) and len(region) == 4 and all(type(v) is int for v in region) and region[2] > 0 and region[3] > 0
            if not valid:
                raise DesktopCaptureError("capture region is invalid", code="invalid_region")
            rx, ry, rw, rh = region
            def contains(display: Mapping[str, Any]) -> bool:
                b = display.get("bounds")
                return isinstance(b, list) and len(b) == 4 and rx >= b[0] and ry >= b[1] and rx + rw <= b[0] + b[2] and ry + rh <= b[1] + b[3]
            match = next((item for item in displays if isinstance(item, Mapping) and item.get("visible") is True and contains(item)), None)
        else:
            candidates = value.get("displays") if target == "display" else value.get("windows")
            identity = request.get("display") if target == "display" else request.get("window_id")
            match = next((item for item in candidates or () if isinstance(item, Mapping) and (item.get("index") == identity if target == "display" else item.get("id") == identity)), None)
        if not match or match.get("visible") is not True:
            raise DesktopCaptureError("capture target is not visible", code="target_unavailable")
        bounds = match.get("bounds")
        if not isinstance(bounds, list) or len(bounds) != 4 or bounds[2] <= 0 or bounds[3] <= 0:
            raise DesktopCaptureError("capture target bounds are invalid", code="target_unavailable")
        return {**dict(match), "visible": True, "viewport": bounds, "crop": request.get("region"), "scale": match.get("scale", 1)}

    def ocr(self, path: Path) -> Mapping[str, Any]:
        executable = self._executable("desktop-ocr", self.root / "native" / "DesktopOCR.swift")
        try:
            result = subprocess.run([str(executable), str(path)], capture_output=True, text=True, timeout=8)
            value = json.loads(result.stdout) if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise DesktopCaptureError("Vision OCR is unavailable", code="ocr_unavailable") from exc
        if not isinstance(value, Mapping):
            raise DesktopCaptureError("Vision OCR returned invalid data", code="ocr_unavailable")
        return dict(value)


class _WindowsDesktopHelpers:
    def metadata(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        from src.windows_desktop_capture import metadata, NativeCaptureError
        try:
            return metadata(request)
        except NativeCaptureError as exc:
            raise DesktopCaptureError(str(exc), code=exc.code) from exc

    async def ocr(self, path: Path) -> Mapping[str, Any]:
        result = await _run_capture(_windows_helper_argv("ocr", str(path)), timeout=8)
        if result["returncode"] != 0 or not isinstance(result.get("value"), Mapping):
            raise DesktopCaptureError("Windows OCR is unavailable", code="ocr_unavailable")
        return result["value"]


def _windows_helper_argv(action: str, *args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        from src.runtime_paths import get_app_root
        root = Path(get_app_root())
        candidates = (root / "bin" / "openclank-windows-desktop-capture.exe", root / "openclank-windows-desktop-capture.exe")
        helper = next((path for path in candidates if path.is_file()), None)
        if helper is None:
            raise DesktopCaptureError("bundled Windows desktop helper is unavailable", code="unavailable")
        return [str(helper), action, *args]
    return [sys.executable, str(Path(__file__).with_name("windows_desktop_capture.py")), action, *args]


async def _await_capture_job(value: Any, cancel_event: asyncio.Event | None) -> Any:
    if not inspect.isawaitable(value):
        return value
    if cancel_event is None:
        return await value
    job = asyncio.ensure_future(value)
    cancellation = asyncio.create_task(cancel_event.wait())
    try:
        await asyncio.wait((job, cancellation), return_when=asyncio.FIRST_COMPLETED)
        if cancel_event.is_set():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            raise DesktopCaptureError("desktop capture was cancelled", code="cancelled")
        return await job
    finally:
        cancellation.cancel()
        if not job.done():
            job.cancel()
        await asyncio.gather(job, cancellation, return_exceptions=True)


class DesktopCaptureError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def capture_enabled(owner: str) -> bool:
    return bool(get_user_setting(CAPTURE_PREF, owner, False))


class _PathUpload:
    content_type = "image/png"

    def __init__(self, path: Path):
        self._file = path.open("rb")

    async def read(self, size: int = -1) -> bytes:
        return self._file.read() if size < 0 else self._file.read(size)


def _persist_capture_metadata_event(database: str, *, owner: str, resource_key: str, resource_revision: Any, metadata: Mapping[str, Any]) -> None:
    with sqlite3.connect(database, timeout=10.0) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS desktop_capture_metadata_events (event_id TEXT PRIMARY KEY, owner TEXT NOT NULL, resource_key TEXT NOT NULL, source_revision TEXT NOT NULL, metadata TEXT NOT NULL, created_at INTEGER NOT NULL)")
        source_key = json.dumps(resource_revision, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        metadata_json = json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event_id = hashlib.sha256((owner + "\0" + resource_key + "\0" + source_key + "\0" + metadata_json).encode()).hexdigest()
        connection.execute("INSERT OR IGNORE INTO desktop_capture_metadata_events(event_id, owner, resource_key, source_revision, metadata, created_at) VALUES (?, ?, ?, ?, ?, strftime('%s','now'))", (event_id, owner, resource_key, source_key, metadata_json))
        connection.commit()


def host_files_importer(*, owner: str, account_id: str, workspace: str, repository: Any = None) -> Callable[..., Any]:
    """Build the trusted owner/account-bound Files host importer for captures."""
    authorized_owner = str(owner).strip()
    if not authorized_owner or not str(account_id).strip() or not str(workspace).strip():
        raise DesktopCaptureError("Files owner binding is incomplete", code="files_unavailable")
    from src.constants import APP_DB
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.files_facade import FilesFacade, ProviderContext
    from src.openclank.files_host_provider import HostFilesProvider, _origin
    from src.openclank.filesystem_registry import FilesystemRootRegistry
    from src.openclank.files_service_client import client_for_owner
    from src.openclank.resource_refs import issue_resource_ref

    repo = repository or FilePolicyRepository(str(os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB))
    provider = HostFilesProvider(
        registry=FilesystemRootRegistry(os.environ.get("ODYSSEUS_FILES_REGISTRY") or None),
        client_factory=client_for_owner,
        operation_store=repo,
    )
    facade = FilesFacade([provider], operation_store=repo)
    captures_ref: str | None = None

    async def _import(path: Path | None, *, owner: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal captures_ref
        request_owner = str(owner).strip()
        metadata_owner = str(metadata.get("owner") or "").strip()
        if request_owner != authorized_owner or metadata_owner != authorized_owner:
            raise DesktopCaptureError("Files owner binding changed", code="files_unavailable")
        context = ProviderContext(
            owner_subject_id=str(account_id), owner_username=authorized_owner,
            policy_generation=repo.generation(), workspace_id=str(workspace),
        )
        parent = _origin(workspace)
        parent_entry = await provider.stat(context, origin_id=parent)
        if parent_entry.kind not in {"folder", "directory"} or "children" not in parent_entry.capabilities or "write" not in parent_entry.capabilities:
            raise DesktopCaptureError("authorized workspace root is not writable", code="files_unavailable")
        parent_ref = issue_resource_ref(
            owner_subject_id=str(account_id), provider="host", origin_id=parent,
            kind=parent_entry.kind, capabilities=parent_entry.capabilities,
            policy_generation=context.policy_generation, workspace_id=context.workspace_id,
        ).token
        digest = hashlib.sha256((str(account_id) + "\0" + str(workspace) + "\0captures").encode()).hexdigest()
        if captures_ref is None:
            directory_receipt = await facade.create_directory(
                context, parent_ref=parent_ref, name="Captures",
                operation_id=f"desktop-captures-{digest[:32]}", generation=context.policy_generation, collision="reuse",
            )
            directory = directory_receipt.get("resource") if isinstance(directory_receipt, Mapping) else None
            if not isinstance(directory, Mapping) or not directory.get("ref"):
                raise DesktopCaptureError("Captures destination has no sealed identity", code="files_unavailable")
            captures_ref = str(directory["ref"])
        if path is None:
            raise DesktopCaptureError("capture revision bytes are not available", code="files_unavailable")
        byte_digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(512 * 1024), b""):
                byte_digest.update(chunk)
        item_digest = hashlib.sha256((str(account_id) + "\0" + str(workspace) + "\0" + byte_digest.hexdigest()).encode()).hexdigest()
        upload = _PathUpload(path)
        try:
            receipt = await facade.import_file(
                context, upload=upload,
                metadata={
                    "generation": context.policy_generation,
                    "operation_id": f"desktop-capture-{item_digest[:32]}",
                    "item_id": item_digest[:32],
                    "destination_ref": captures_ref,
                    "name": f"capture-{byte_digest.hexdigest()[:20]}.png",
                    "collision": "fail",
                },
            )
        finally:
            upload._file.close()
        items = receipt.get("items") if isinstance(receipt, Mapping) else None
        item = items[0] if isinstance(items, list) and items and isinstance(items[0], Mapping) else None
        if not item or not item.get("resource_ref"):
            raise DesktopCaptureError("Files import returned no durable identity", code="files_unavailable")
        resource_key = item["resource_ref"]
        resource_revision = item.get("revision")
        _persist_capture_metadata_event(repo.db_path, owner=authorized_owner, resource_key=resource_key, resource_revision=resource_revision, metadata=metadata)
        return {"resource_key": resource_key, "revision": resource_revision, "workspace_id": context.workspace_id, "owner": authorized_owner}

    async def _validate_resource(resource: Mapping[str, Any], request_owner: str) -> Mapping[str, Any] | None:
        if str(request_owner).strip() != authorized_owner or str(resource.get("owner") or "").strip() != authorized_owner:
            return None
        token = str(resource.get("resource_key") or "").strip()
        if not token:
            return False
        context = ProviderContext(owner_subject_id=str(account_id), owner_username=authorized_owner, policy_generation=repo.generation(), workspace_id=str(workspace))
        try:
            resolved_provider, resolved = facade._provider_for_ref(context, token, capability="stat")
            entry = await resolved_provider.stat(context, origin_id=resolved.origin_id)
            if entry.kind != "file":
                return None
            with sqlite3.connect(repo.db_path) as connection:
                rows = connection.execute("SELECT source_revision, metadata FROM desktop_capture_metadata_events WHERE owner = ? AND resource_key = ? ORDER BY event_id ASC", (authorized_owner, token)).fetchall()
            if not rows:
                return None
            expected = json.dumps(entry.revision, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            matching = next((row for row in rows if row[0] == expected), None)
            if matching is None:
                return None
            source_revision, metadata_json = matching
            value = json.loads(metadata_json)
            return {"metadata": value, "source_revision": json.loads(source_revision)}
        except Exception:
            return None

    _import.validate_resource = _validate_resource

    return _import


def _argv(request: Mapping[str, Any], output: str, metadata: Mapping[str, Any] | None = None) -> list[str]:
    target = str(request.get("target") or "display")
    args = ["/usr/sbin/screencapture", "-x", "-t", "png"]
    if target == "display":
        display = request.get("display")
        if type(display) is not int or not 1 <= display <= 32:
            raise DesktopCaptureError("display identity is required", code="invalid_display")
        args += ["-D", str(display)]
    elif target == "window":
        window = request.get("window_id")
        if type(window) is not int or window <= 0:
            raise DesktopCaptureError("window identity is required", code="invalid_window")
        args += ["-l", str(window)]
    elif target == "region":
        region = request.get("region")
        if not isinstance(region, (list, tuple)) or len(region) != 4 or any(type(v) is not int for v in region) or region[2] <= 0 or region[3] <= 0:
            raise DesktopCaptureError("region must be four nonnegative integers", code="invalid_region")
        args += ["-R", ",".join(str(v) for v in region)]
    else:
        raise DesktopCaptureError("capture target is unsupported", code="unsupported_target")
    if sys.platform == "win32":
        if metadata is None:
            raise DesktopCaptureError("Windows capture target metadata is required", code="target_unavailable")
        return _windows_helper_argv("capture", json.dumps(dict(request)), output, json.dumps(dict(metadata)))
    return args + [output]


async def capture_desktop(
    request: Mapping[str, Any],
    *,
    owner: str,
    runner: Callable[..., Any] | None = None,
    importer: Callable[..., Any] | None = None,
    ocr_runner: Callable[..., Any] | None = None,
    metadata_reader: Callable[..., Any] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    if not owner:
        raise DesktopCaptureError("desktop capture requires an owner", code="owner_required")
    if not capture_enabled(owner):
        raise DesktopCaptureError("desktop capture is disabled for this owner", code="opt_in_required")
    if cancel_event and cancel_event.is_set():
        raise DesktopCaptureError("desktop capture was cancelled", code="cancelled")
    runner = runner or _run_capture
    target_metadata = None
    if metadata_reader is not None:
        target_metadata = metadata_reader(request)
        if not isinstance(target_metadata, Mapping) or target_metadata.get("visible") is not True:
            raise DesktopCaptureError("capture target is not visible", code="target_unavailable")
        bounds = target_metadata.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4 or bounds[2] <= 0 or bounds[3] <= 0:
            raise DesktopCaptureError("capture target bounds are invalid", code="target_unavailable")
    with tempfile.TemporaryDirectory(prefix="openclank-capture-") as temp_dir:
        output = str(Path(temp_dir) / "capture.png")
        argv = _argv(request, output, target_metadata)
        try:
            result = runner(argv, timeout=CAPTURE_TIMEOUT_SECONDS)
            result = await _await_capture_job(result, cancel_event)
        except (subprocess.TimeoutExpired, asyncio.TimeoutError) as exc:
            raise DesktopCaptureError("desktop capture timed out", code="timeout") from exc
        except asyncio.CancelledError as exc:
            raise DesktopCaptureError("desktop capture was cancelled", code="cancelled") from exc
        except PermissionError as exc:
            raise DesktopCaptureError("screen recording permission is unavailable", code="permission_required") from exc
        except OSError as exc:
            raise DesktopCaptureError("desktop capture is unavailable on this host", code="unavailable") from exc
        if cancel_event and cancel_event.is_set():
            raise DesktopCaptureError("desktop capture was cancelled", code="cancelled")
        if isinstance(result, Mapping) and int(result.get("returncode", 0)) != 0:
            code = str(result.get("value", {}).get("error", "permission_required")) if isinstance(result.get("value"), Mapping) else "permission_required"
            raise DesktopCaptureError("desktop capture failed", code=code)
        path = Path(output)
        if not path.is_file():
            raise DesktopCaptureError("desktop capture produced no image", code="unavailable")
        size = path.stat().st_size
        if size <= 0 or size > MAX_CAPTURE_BYTES or not _valid_png(path):
            raise DesktopCaptureError("desktop capture size is invalid", code="size_limit")
        ocr = None
        if ocr_runner is not None:
            ocr = ocr_runner(path)
            try:
                ocr = await _await_capture_job(ocr, cancel_event)
            except asyncio.CancelledError as exc:
                raise DesktopCaptureError("desktop capture was cancelled", code="cancelled") from exc
            except asyncio.TimeoutError as exc:
                raise DesktopCaptureError("desktop OCR timed out", code="timeout") from exc
        if cancel_event and cancel_event.is_set():
            raise DesktopCaptureError("desktop capture was cancelled", code="cancelled")
        if importer is None:
            raise DesktopCaptureError("Files import is unavailable", code="files_unavailable")
        metadata = {"kind": "desktop_capture", "owner": owner, "source": request.get("target", "display"), "target": target_metadata, "ocr": ocr}
        imported = importer(path, owner=owner, metadata=metadata)
        if inspect.isawaitable(imported):
            imported = await imported
        if not isinstance(imported, Mapping) or not imported.get("resource_key"):
            raise DesktopCaptureError("Files import returned no durable identity", code="files_unavailable")
        return {"state": "captured", "resource": dict(imported), "source": str(request.get("target") or "display"), "target": target_metadata, "ocr": ocr}


async def correct_capture(
    original: Mapping[str, Any], *, owner: str, text: str, boxes: list[dict[str, Any]],
    revision_store: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    original_id = str(original.get("resource_key") or "")
    if not original_id:
        raise DesktopCaptureError("immutable original identity is required", code="invalid_original")
    bound_owner = str(original.get("owner") or "").strip()
    if bound_owner and bound_owner != str(owner).strip():
        raise DesktopCaptureError("immutable original belongs to another owner", code="invalid_original")
    source_revision = original.get("revision")
    revision = 1
    metadata = {"kind": "desktop_ocr_correction", "original_id": original_id, "source_revision": source_revision, "revision": revision, "text": text, "boxes": boxes}
    if revision_store is not None:
        result = revision_store(metadata)
        if inspect.isawaitable(result):
            result = await result
    else:
        # Correction is metadata lineage, never a fabricated second file.
        from src.constants import APP_DB
        database = str(os.environ.get("OPEN_CLANK_AUTHORITY_DB_PATH") or APP_DB)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS desktop_capture_revisions (request_digest TEXT PRIMARY KEY, owner TEXT NOT NULL, original_id TEXT NOT NULL, source_revision TEXT NOT NULL, revision INTEGER NOT NULL, recognized TEXT NOT NULL, text TEXT NOT NULL, boxes TEXT NOT NULL, created_at INTEGER NOT NULL)")
            source_key = json.dumps(source_revision, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            boxes_json = json.dumps(boxes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            recognized = json.dumps(original.get("ocr") or original.get("recognized") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            request_digest = hashlib.sha256((str(owner) + "\0" + original_id + "\0" + source_key + "\0" + str(text) + "\0" + boxes_json).encode()).hexdigest()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision FROM desktop_capture_revisions WHERE request_digest = ?", (request_digest,)).fetchone()
            if row:
                revision = int(row[0])
            else:
                row = connection.execute("SELECT COALESCE(MAX(revision), 0) FROM desktop_capture_revisions WHERE owner = ? AND original_id = ?", (str(owner), original_id)).fetchone()
                revision = int(row[0] or 0) + 1
                connection.execute("INSERT INTO desktop_capture_revisions(request_digest, owner, original_id, source_revision, revision, recognized, text, boxes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))", (request_digest, str(owner), original_id, source_key, revision, recognized, str(text), boxes_json))
            metadata["revision"] = revision
            connection.commit()
        result = {"revision_id": f"desktop-ocr:{hashlib.sha256((str(owner) + '\0' + original_id + '\0' + str(revision)).encode()).hexdigest()[:32]}", "original_id": original_id, "revision": revision}
    if not isinstance(result, Mapping) or not result.get("revision_id"):
        raise DesktopCaptureError("correction metadata could not be persisted", code="files_unavailable")
    return {"state": "corrected", "original_id": original_id, "revision": revision, "metadata_revision": dict(result)}


def _valid_png(path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
            if image.width <= 0 or image.height <= 0 or image.format != "PNG":
                return False
        with path.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            length = struct.unpack(">I", stream.read(4))[0]
            if length != 13 or stream.read(4) != b"IHDR":
                return False
            width, height = struct.unpack(">II", stream.read(8))
            return width > 0 and height > 0
    except (OSError, struct.error):
        return False


async def _run_capture(argv: list[str], *, timeout: int) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=(os.name == "posix"),
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
    )
    try:
        _stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1)
            except asyncio.TimeoutError:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        raise
    result: dict[str, Any] = {"returncode": int(process.returncode or 0)}
    if sys.platform == "win32":
        try:
            result["value"] = json.loads(_stdout)
        except (ValueError, UnicodeError):
            pass
    return result


async def capture_tool(content: str, ctx: Mapping[str, Any]) -> dict[str, Any]:
    request = json.loads(content or "{}")
    if not isinstance(request, dict):
        raise DesktopCaptureError("capture request must be an object", code="invalid_request")
    owner = str(ctx.get("owner") or "")
    importer = ctx.get("files_importer")
    caller_supplied_importer = importer is not None
    if importer is None:
        try:
            from src.tool_execution import _copal_account_id, _trusted_workspace_from_id
            _username, account_id, identity_error = _copal_account_id(owner)
            workspace = _trusted_workspace_from_id(
                str(ctx.get("workspace") or ""), owner=owner,
                authority_workspace_id=str(ctx.get("authority_workspace_id") or ""),
            )
            if identity_error or not account_id or not workspace:
                raise DesktopCaptureError(identity_error or "authorized workspace is required", code="files_unavailable")
            importer = host_files_importer(owner=owner, account_id=account_id, workspace=workspace)
        except DesktopCaptureError:
            raise
        except Exception as exc:
            raise DesktopCaptureError("Files importer is unavailable", code="files_unavailable") from exc
    metadata_reader = None
    ocr_runner = None
    if sys.platform == "darwin" and not caller_supplied_importer:
        helpers = _NativeDesktopHelpers()
        metadata_reader = helpers.metadata
        ocr_runner = helpers.ocr
    elif sys.platform == "win32" and not caller_supplied_importer:
        helpers = _WindowsDesktopHelpers()
        metadata_reader = helpers.metadata
        ocr_runner = helpers.ocr
    if request.get("action") == "correct":
        original = request.get("original")
        if not isinstance(original, Mapping) or not isinstance(request.get("text"), str) or not isinstance(request.get("boxes", []), list):
            raise DesktopCaptureError("correction request is invalid", code="invalid_request")
        validator = getattr(importer, "validate_resource", None)
        authoritative = await validator(original, owner) if callable(validator) else None
        if not isinstance(authoritative, Mapping):
            raise DesktopCaptureError("immutable original is unavailable", code="invalid_original")
        original = {**dict(original), "ocr": authoritative.get("metadata", {}).get("ocr"), "revision": authoritative.get("source_revision")}
        return await correct_capture(original, owner=owner, text=request["text"], boxes=request["boxes"])
    result = await capture_desktop(request, owner=owner, importer=importer, metadata_reader=metadata_reader, ocr_runner=ocr_runner)
    return result
