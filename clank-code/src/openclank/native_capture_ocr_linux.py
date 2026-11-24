"""Installed-only, bounded local OCR for a temporary desktop capture."""
from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import shutil
import signal
from pathlib import Path

LANGUAGE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
MAX_OUTPUT = 4 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
MAX_BOXES = 10000
MAX_TEXT = 100000


class LinuxOCRError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def _invoke(executable: str, args: list[str], *, timeout: int, limit: int) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(executable, *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=65536)
    except OSError as exc:
        raise LinuxOCRError("ocr_unavailable") from exc
    try:
        async def collect() -> bytes:
            assert process.stdout is not None
            result = bytearray()
            while True:
                part = await process.stdout.read(min(65536, limit + 1 - len(result)))
                if not part:
                    break
                result.extend(part)
                if len(result) > limit:
                    raise LinuxOCRError("ocr_output_limit")
            if await process.wait() != 0:
                raise LinuxOCRError("ocr_unavailable")
            return bytes(result)
        return await asyncio.wait_for(collect(), timeout)
    except asyncio.TimeoutError as exc:
        raise LinuxOCRError("ocr_timeout") from exc
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()


async def readiness() -> dict:
    executable = shutil.which("tesseract")
    if not executable:
        return {"state": "unavailable", "engine": "tesseract", "supportedLanguages": [], "reason": "engine_missing"}
    try:
        output = await _invoke(executable, ["--list-langs"], timeout=3, limit=65536)
        lines = output.decode("utf-8", "strict").splitlines()
        languages = sorted(set(line.strip() for line in lines[1:] if LANGUAGE.fullmatch(line.strip())))[:256]
    except (LinuxOCRError, UnicodeError):
        return {"state": "unavailable", "engine": "tesseract", "supportedLanguages": [], "reason": "language_probe_failed"}
    return {"state": "ready" if languages else "unavailable", "engine": "tesseract",
            "supportedLanguages": languages, "reason": None if languages else "no_languages"}


async def recognize(path: Path, language: str = "eng") -> dict:
    if not isinstance(language, str) or not LANGUAGE.fullmatch(language):
        raise LinuxOCRError("ocr_invalid_language")
    available = await readiness()
    executable = shutil.which("tesseract")
    if not executable or available["state"] != "ready":
        raise LinuxOCRError("ocr_unavailable")
    if language not in available["supportedLanguages"]:
        raise LinuxOCRError("ocr_language_unavailable")
    try:
        from PIL import Image
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise LinuxOCRError("ocr_image_limit")
    except (OSError, ValueError) as exc:
        if isinstance(exc, LinuxOCRError):
            raise
        raise LinuxOCRError("ocr_unavailable") from exc
    data = await _invoke(executable, [str(path), "stdout", "-l", language, "tsv"], timeout=8, limit=MAX_OUTPUT)
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8", "strict")), delimiter="\t")
        boxes, total = [], 0
        for row in reader:
            if row.get("level") != "5":
                continue
            text = (row.get("text") or "").strip()
            if not text:
                continue
            x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            confidence = float(row["conf"])
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height or not 0 <= confidence <= 100:
                raise ValueError("invalid box")
            total += len(text) + 1
            if len(boxes) >= MAX_BOXES or total > MAX_TEXT:
                raise LinuxOCRError("ocr_output_limit")
            boxes.append({"text": text, "confidence": confidence / 100, "candidates": [text],
                          "x": x, "y": y, "width": w, "height": h, "order": len(boxes)})
    except (UnicodeError, csv.Error, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, LinuxOCRError):
            raise
        raise LinuxOCRError("ocr_invalid_output") from exc
    return {"supportedLanguages": available["supportedLanguages"], "language": language, "width": width,
            "height": height, "text": " ".join(box["text"] for box in boxes), "boxes": boxes}
