"""Bounded read-only IWI v6 texture decoding using Pillow's BCn codec.

The extension is a hint, never authority. This module consumes bytes already
obtained under the caller's resource authority and does not open files.
"""
from __future__ import annotations

import hashlib
import io
import struct
from dataclasses import dataclass

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 16 * 1024 * 1024
MAX_PNG_BYTES = 64 * 1024 * 1024
ADAPTER_VERSION = "iwi6-bcn-1"
_HEADER = struct.Struct("<3sBBBHHHIIII")
_FORMATS = {0x0B: ("DXT1", 8, 1), 0x0C: ("DXT3", 16, 2), 0x0D: ("DXT5", 16, 3)}


class IwiUnsupported(ValueError):
    """The supplied data is unsupported or violates the bounded format."""


@dataclass(frozen=True)
class IwiInfo:
    width: int
    height: int
    variant: str
    source_sha256: str
    mip0_start: int
    mip0_end: int
    decoder_generation: str


def decoder_available() -> bool:
    try:
        import PIL
        from PIL import Image
        return int(PIL.__version__.split(".", 1)[0]) == 12 and hasattr(Image.core, "bcn_decoder")
    except (ImportError, OSError, ValueError, AttributeError):
        return False


class _BoundedOutput(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_PNG_BYTES:
            raise IwiUnsupported("Derived PNG exceeds the preview limit")
        return super().write(data)


def inspect(data: bytes) -> IwiInfo:
    """Validate a complete finite IWI6 2D DXT image without decoding it."""
    if not isinstance(data, bytes) or not _HEADER.size <= len(data) <= MAX_INPUT_BYTES:
        raise IwiUnsupported("IWI input size is unsupported")
    magic, version, fmt, flags, width, height, depth, *extents = _HEADER.unpack_from(data)
    if magic != b"IWi" or version != 6 or fmt not in _FORMATS:
        raise IwiUnsupported("Only IWI version 6 DXT1, DXT3 and DXT5 are supported")
    # These are the fully specified no-mip and full-mip layouts. Other flags,
    # including cubemap, volume and mixed partial-mip modes, remain unsupported.
    if flags not in (0, 3) or depth != 1:
        raise IwiUnsupported("Only complete 2D IWI mip layouts are supported")
    if not width or not height or width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise IwiUnsupported("IWI dimensions exceed the preview limit")
    name, block_bytes, _ = _FORMATS[fmt]
    levels: list[int] = []
    w, h = width, height
    while True:
        levels.append(((w + 3) // 4) * ((h + 3) // 4) * block_bytes)
        if flags == 3 or (w == 1 and h == 1):
            break
        w, h = max(1, w // 2), max(1, h // 2)
    total = _HEADER.size + sum(levels)
    expected = []
    end = total
    for index in range(4):
        expected.append(end)
        if index + 1 < len(levels):
            end -= levels[index]
    if total != len(data) or extents != expected:
        raise IwiUnsupported("IWI mip extents are inconsistent or truncated")
    try:
        import PIL
        if not decoder_available():
            raise IwiUnsupported("The supported Pillow BCn decoder is unavailable")
        generation = f"{ADAPTER_VERSION}:pillow-{PIL.__version__}"
    except ImportError as exc:
        raise IwiUnsupported("The Pillow BCn decoder is unavailable") from exc
    return IwiInfo(width, height, name, hashlib.sha256(data).hexdigest(), total - levels[0], total, generation)


def _decode(data: bytes):
    """Return validated metadata and an owned decoded image."""
    info = inspect(data)
    from PIL import Image
    decoder = next(value[2] for value in _FORMATS.values() if value[0] == info.variant)
    image = Image.frombytes("RGBA", (info.width, info.height), data[info.mip0_start:info.mip0_end], "bcn", (decoder, info.variant))
    return info, image


def preview_png(data: bytes) -> tuple[IwiInfo, bytes]:
    """Return a bounded derived PNG, preserving the caller's original bytes."""
    try:
        info, image = _decode(data)
        with image, _BoundedOutput() as output:
            image.save(output, format="PNG")
            if output.tell() > MAX_PNG_BYTES:
                raise IwiUnsupported("Derived PNG exceeds the preview limit")
            png = output.getvalue()
    except IwiUnsupported:
        raise
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise IwiUnsupported("The bounded Pillow BCn decoder could not decode this image") from exc
    return info, png


def thumbnail_png(data: bytes, width: int, height: int, scale: float) -> tuple[IwiInfo, bytes]:
    try:
        from PIL import Image
        max_width = max(1, min(1024, round(width * scale)))
        max_height = max(1, min(1024, round(height * scale)))
        info, image = _decode(data)
        with image:
            image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            with _BoundedOutput() as output:
                image.save(output, format="PNG")
                if output.tell() > 4 * 1024 * 1024:
                    raise IwiUnsupported("Derived thumbnail exceeds its limit")
                return info, output.getvalue()
    except IwiUnsupported:
        raise
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        raise IwiUnsupported("The bounded IWI thumbnail is unavailable") from exc
