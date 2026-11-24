"""Bounded, inactive model-image derivative. This module grants no admission.

Callers must independently prove current source, route, settings and calibrated
profile before using a result. Failure leaves the original input unchanged.
"""
from __future__ import annotations

import hashlib
import io
import warnings
from .media_input_policy import HEAD_BYTES, evaluate

MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_DECODE_PIXELS = 16_000_000
MAX_DERIVED_BYTES = 256 * 1024
MAX_SIDE = 2048
PROFILE = 'image-downscale-v1'
TRANSFORM = 'pillow-lanczos-v1'


class _BoundedBytes(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_DERIVED_BYTES:
            raise ValueError('derived image exceeds bound')
        return super().write(data)


def derive_static_image(original: bytes, *, upload_id: str, media_type: str) -> tuple[dict, bytes] | None:
    """Return an ephemeral candidate, or None without changing original bytes."""
    if (type(original) is not bytes or not 0 < len(original) <= MAX_SOURCE_BYTES
            or type(upload_id) is not str or not upload_id or len(upload_id) > 256
            or media_type not in ('image/png', 'image/jpeg', 'image/webp')):
        return None
    receipt = evaluate(declared_type=media_type, byte_size=len(original), head=original[:HEAD_BYTES],
                       route_has_vision=True, max_bytes=MAX_SOURCE_BYTES)
    if (receipt['outcome'] != 'admit' or receipt['animated'] is not False
            or not receipt['width'] or not receipt['height']
            or receipt['width'] * receipt['height'] > MAX_DECODE_PIXELS):
        return None
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(original)) as source:
                if (source.format not in ('PNG', 'JPEG', 'WEBP') or getattr(source, 'n_frames', 1) != 1
                        or source.size != (receipt['width'], receipt['height'])):
                    return None
                if source.info.get('icc_profile'):
                    return None
                exif = source.getexif()
                if source.info.get('exif') and not exif:
                    return None
                orientation = exif.get(274, 1)
                if type(orientation) is not int or orientation != 1:
                    return None
                source.load()
                oriented = source
                if max(oriented.size) <= MAX_SIDE:
                    return None
                oriented.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                # PNG preserves transparency; no metadata or external file is carried.
                output = oriented.convert('RGBA' if 'A' in oriented.getbands() or 'transparency' in oriented.info else 'RGB')
                sink = _BoundedBytes()
                output.save(sink, format='PNG', optimize=False)
                data = sink.getvalue()
                if not data or len(data) >= len(original):
                    return None
                result = {'version': 1, 'profile': PROFILE, 'uploadID': upload_id,
                          'originalSHA256': hashlib.sha256(original).hexdigest(),
                          'originalMime': media_type, 'originalWidth': receipt['width'], 'originalHeight': receipt['height'],
                          'derivedSHA256': hashlib.sha256(data).hexdigest(), 'derivedMime': 'image/png',
                          'derivedWidth': output.width, 'derivedHeight': output.height,
                          'derivedLength': len(data), 'transformVersion': TRANSFORM}
                return result, data
    except (ImportError, OSError, ValueError, SyntaxError, MemoryError, Warning):
        return None
