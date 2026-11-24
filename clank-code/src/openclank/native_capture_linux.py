"""Bounded Linux Screenshot-portal subprocess transport (protocol v1)."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

MAX_PNG = 64 * 1024 * 1024
MAX_HEADER = 8192

class NativeCaptureError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

def _helper() -> Path:
    if getattr(sys, "frozen", False):
        from src.runtime_paths import get_app_root
        return Path(get_app_root()) / "bin" / "openclank-native-capture"
    return Path(__file__).resolve().parents[2] / "native/capture/target/release/openclank-native-capture"

async def run(action: str, request: Mapping[str, Any], owner: str, output: Path | None = None) -> dict[str, Any]:
    """A portal selection applies only to this one request, never a window lease."""
    if not owner or len(owner) > 256 or request.get("target", "portal") != "portal":
        raise NativeCaptureError("unsupported_target")
    crop = request.get("crop")
    scale = request.get("scale", 1.0)
    if crop is not None and (not isinstance(crop, (list, tuple)) or len(crop) != 4 or any(type(x) is not int or x < 0 or x > 16384 for x in crop)):
        raise NativeCaptureError("invalid_crop")
    if type(scale) not in (int, float) or not 0.05 <= scale <= 1:
        raise NativeCaptureError("invalid_scale")
    generation = uuid.uuid4().hex
    timeout = 15
    payload = {"version": 1, "action": action, "owner": owner, "target": "portal", "target_generation": generation,
               "deadline_ms": int(time.time() * 1000) + timeout * 1000, "crop": crop, "scale": scale}
    helper = _helper()
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise NativeCaptureError("unavailable")
    try:
        process = await asyncio.create_subprocess_exec(str(helper), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=MAX_HEADER + 1)
    except OSError as exc:
        raise NativeCaptureError("unavailable") from exc
    try:
        async def exchange() -> tuple[bytes, bytes]:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps(payload, separators=(",", ":")).encode())
            await process.stdin.drain()
            process.stdin.close()
            header = await process.stdout.readline()
            if not header.endswith(b"\n") or len(header) > MAX_HEADER:
                raise NativeCaptureError("invalid_response")
            try:
                info = json.loads(header)
            except (ValueError, UnicodeError) as exc:
                raise NativeCaptureError("invalid_response") from exc
            if not isinstance(info, dict) or info.get("version") != 1 or info.get("target_generation") != generation:
                raise NativeCaptureError("invalid_response")
            size = info.get("png_bytes")
            if type(size) is not int or not 0 <= size <= MAX_PNG:
                raise NativeCaptureError("invalid_response")
            data = await process.stdout.readexactly(size)
            if await process.stdout.read(1):
                raise NativeCaptureError("invalid_response")
            await process.wait()
            return header, data
        header, data = await asyncio.wait_for(exchange(), timeout=timeout + 1)
    except (asyncio.TimeoutError, asyncio.CancelledError, asyncio.IncompleteReadError, ValueError) as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, NativeCaptureError):
            raise
        raise NativeCaptureError("timeout" if isinstance(exc, asyncio.TimeoutError) else "invalid_response") from exc
    finally:
        if process.returncode is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await process.wait()
    info = json.loads(header)
    if process.returncode != 0 or info.get("state") == "error":
        raise NativeCaptureError(str(info.get("error") or "unavailable"))
    if action == "screenshot":
        if info.get("state") != "captured" or not data or output is None:
            raise NativeCaptureError("invalid_response")
        source, crop, size = info.get("source_size"), info.get("crop"), info.get("output_size")
        if (info.get("backend") != "xdg-desktop-portal-screenshot/1" or info.get("stream") is not False
                or info.get("retained_target") is not False or type(info.get("source_time_ms")) is not int
                or any(not isinstance(v, list) or len(v) != n or any(type(x) is not int or x < 0 or x > 16384 for x in v)
                       for v, n in ((source, 2), (crop, 4), (size, 2)))
                or not all(source) or not all(size) or not crop[2] or not crop[3]
                or crop[0] + crop[2] > source[0] or crop[1] + crop[3] > source[1]
                or len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n"
                or data[12:16] != b"IHDR" or list(struct.unpack(">II", data[16:24])) != size):
            raise NativeCaptureError("invalid_response")
        output.write_bytes(data)
    elif data or info.get("state") not in ("unverified", "unavailable"):
        raise NativeCaptureError("invalid_response")
    return info
