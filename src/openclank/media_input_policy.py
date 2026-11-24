"""Image input admission policy (media-input-v1): admit the original bytes or omit.

Pure header parsing on the first bytes of a file; never decodes pixels, never
produces derived bytes, never does I/O. Standard library only.
"""

import re

POLICY = "media-input-v1"

# shortcut: same value as DEFAULT_CHAT_UPLOAD_MAX_BYTES (src/upload_limits.py:8); that module
# imports fastapi, too heavy here. It ignores the OPEN_CLANK_CHAT_UPLOAD_MAX_BYTES override; the
# wiring packet should pass the effective cap if one is wanted.
MAX_BYTES = 10 * 1024 * 1024
# Pillow's default decompression-bomb warning threshold, checked from the header only.
MAX_PIXELS = 89_478_485
# Largest side any supported container can legitimately carry (JPEG/WebP-lossy 16-bit limit).
MAX_SIDE = 65_535
# Only this many leading bytes are ever looked at.
HEAD_BYTES = 64 * 1024
# shortcut: animated GIF/WebP frame count is not parsed; they pass or fail on the same size rules.
# Upgrade when a frame cap is decided.

ACCEPTED_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
REASONS = (
    "too_large_bytes", "too_many_pixels", "side_too_long", "unsupported_type",
    "type_mismatch", "unreadable_header", "route_without_vision",
)

# Reasons that block at a site that does NOT decode locally (model request, describe): header
# problems (unreadable_header/type_mismatch/unsupported_type) are not blocking there.
NON_DECODE_BLOCKING = frozenset(
    {"too_many_pixels", "side_too_long", "too_large_bytes", "route_without_vision"})

# Plain words for each closed reason, and what to do about it. Shared by the upload log, the model-request note and the describe result;
# static/js/mediaInputNotice.js mirrors the words for the chat attachment card.
REASON_TEXT = {
    "too_large_bytes": "the file is larger than the allowed size",
    "too_many_pixels": "the image has too many pixels to decode safely",
    "side_too_long": "one side of the image is longer than the allowed limit",
    "unsupported_type": "this image format is not one the model input policy accepts",
    "type_mismatch": "the file contents do not match its declared type",
    "unreadable_header": "the image header could not be read",
    "route_without_vision": "the selected model does not accept images",
}
ALTERNATIVE = {
    "route_without_vision": "Choose a model that accepts images, or use Caption on the image to have it described.",
    "unsupported_type": "Convert it to PNG, JPEG, WebP or GIF, or use Caption on the image to have it described.",
}


def reason_text(reason):
    return REASON_TEXT.get(reason, "the image could not be admitted")


_MIME_RE = re.compile(r"^[a-z0-9.+-]{1,32}/[a-z0-9.+-]{1,32}$")


def _png(h):
    if len(h) < 24 or h[12:16] != b"IHDR":
        return None, None
    return int.from_bytes(h[16:20], "big"), int.from_bytes(h[20:24], "big")


def _gif(h):
    if len(h) < 10:
        return None, None
    return int.from_bytes(h[6:8], "little"), int.from_bytes(h[8:10], "little")


def _jpeg(h):
    n, i = len(h), 2
    while i + 4 <= n:
        if h[i] != 0xFF:
            return None, None
        m = h[i + 1]
        if m == 0xFF:  # fill byte
            i += 1
            continue
        if m == 0x01 or 0xD0 <= m <= 0xD8:  # standalone, no length
            i += 2
            continue
        if m in (0xD9, 0xDA):  # EOI/SOS before any SOF
            return None, None
        seg = int.from_bytes(h[i + 2:i + 4], "big")
        if seg < 2 or i + 2 + seg > n:
            return None, None
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            if seg < 8:
                return None, None
            return int.from_bytes(h[i + 7:i + 9], "big"), int.from_bytes(h[i + 5:i + 7], "big")
        i += 2 + seg  # strictly increases, so the loop terminates
    return None, None


def _webp(h):
    kind = h[12:16]
    if kind == b"VP8 ":
        if len(h) < 30 or h[23:26] != b"\x9d\x01\x2a":
            return None, None
        return int.from_bytes(h[26:28], "little") & 0x3FFF, int.from_bytes(h[28:30], "little") & 0x3FFF
    if kind == b"VP8L":
        if len(h) < 25 or h[20] != 0x2F:
            return None, None
        bits = int.from_bytes(h[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        if len(h) < 30:
            return None, None
        return int.from_bytes(h[24:27], "little") + 1, int.from_bytes(h[27:30], "little") + 1
    return None, None


def _animated(kind, h):
    """True/False only when the container header proves it; None when it cannot (GIF headers never prove frame count). No frame decode."""
    if kind == "image/jpeg":
        return False
    if kind == "image/webp":
        return bool(h[20] & 0x02) if h[12:16] == b"VP8X" and len(h) > 20 else (False if h[12:16] in (b"VP8 ", b"VP8L") else None)
    if kind == "image/png":
        pos = 8
        while pos + 8 <= len(h):
            ctype = h[pos + 4:pos + 8]
            if ctype == b"acTL":
                return True
            if ctype == b"IDAT":
                return False
            pos += 12 + int.from_bytes(h[pos:pos + 4], "big")
    return None


def sniff(head):
    """Return (media_type | None, width | None, height | None) from container headers only."""
    h = bytes(head[:HEAD_BYTES])
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        kind, parse = "image/png", _png
    elif h[:2] == b"\xff\xd8":
        kind, parse = "image/jpeg", _jpeg
    elif h[:6] in (b"GIF87a", b"GIF89a"):
        kind, parse = "image/gif", _gif
    elif h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        kind, parse = "image/webp", _webp
    else:
        return None, None, None
    w, h2 = parse(h)
    if not w or not h2:  # None or zero: malformed
        return kind, None, None
    return kind, w, h2


def _norm(declared):
    t = declared.split(";", 1)[0].strip().lower() if isinstance(declared, str) else ""
    return "image/jpeg" if t == "image/jpg" else t


def evaluate(*, declared_type, byte_size, head, route_has_vision, max_bytes=None):
    """Return the receipt dict for one attachment. Never raises on bad input bytes.

    max_bytes: the effective upload cap the caller already enforces (default MAX_BYTES).
    """
    cap = MAX_BYTES if max_bytes is None else max_bytes
    declared = _norm(declared_type)
    sniffed, w, h = None, None, None
    reason = None
    animated = None
    if declared not in ACCEPTED_TYPES:
        reason = "unsupported_type"
    elif route_has_vision is not True:
        reason = "route_without_vision"
    elif not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
        reason = "unreadable_header"
    elif byte_size > cap:
        reason = "too_large_bytes"
    else:
        try:
            sniffed, w, h = sniff(head)
            animated = _animated(sniffed, bytes(head[:HEAD_BYTES])) if sniffed else None
        except Exception:  # non-bytes head etc.: omit, never guess
            sniffed, w, h, animated = None, None, None, None
        if sniffed is None:
            reason = "unreadable_header"
        elif sniffed != declared:
            reason = "type_mismatch"
        elif w is None:
            reason = "unreadable_header"
        else:
            reason = _size_reason(w, h)
    return _receipt(reason, sniffed, declared, byte_size, w, h, cap, animated)


def _size_reason(w, h):
    if max(w, h) > MAX_SIDE:
        return "side_too_long"
    return "too_many_pixels" if w * h > MAX_PIXELS else None


def receipt_for(declared_type, byte_size, width, height, max_bytes=None, reason=None):
    """Receipt for dimensions a caller already knows (e.g. from Pillow's header-only open).

    reason: force an omit with that code (e.g. the decoder itself refused the size).
    """
    cap = MAX_BYTES if max_bytes is None else max_bytes
    return _receipt(reason or _size_reason(width, height), None, _norm(declared_type),
                    byte_size, width, height, cap)


def _receipt(reason, sniffed, declared, byte_size, w, h, cap, animated=None):
    media = sniffed or (declared if _MIME_RE.match(declared) else None)
    return {
        "policy": POLICY,
        "outcome": "omit" if reason else "admit",
        "reason": reason,
        "media_type": media,
        "byte_size": byte_size if isinstance(byte_size, int) and not isinstance(byte_size, bool) else None,
        "width": w,
        "height": h,
        "animated": animated,
        "limits": {"max_bytes": cap, "max_pixels": MAX_PIXELS, "max_side": MAX_SIDE},
    }
