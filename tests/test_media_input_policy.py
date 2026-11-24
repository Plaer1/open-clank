import ast
import pathlib
import struct
import sys

from src.openclank import media_input_policy as p

KEYS = {"policy", "outcome", "reason", "media_type", "byte_size", "width", "height", "animated", "limits"}


def png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"


def jpeg(w, h):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", h, w) + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof


def gif(w, h):
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00"


def riff(chunk):
    return b"RIFF" + struct.pack("<I", len(chunk) + 4) + b"WEBP" + chunk


def webp_vp8(w, h):
    return riff(b"VP8 " + struct.pack("<I", 10) + b"\x00\x00\x00\x9d\x01\x2a" + struct.pack("<HH", w, h))


def webp_vp8l(w, h):
    bits = (w - 1) | ((h - 1) << 14)
    return riff(b"VP8L" + struct.pack("<I", 5) + b"\x2f" + struct.pack("<I", bits))


def webp_vp8x(w, h):
    return riff(b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
                + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))


def ev(mime, head, size=1000, vision=True, max_bytes=None):
    return p.evaluate(declared_type=mime, byte_size=size, head=head, route_has_vision=vision, max_bytes=max_bytes)


CASES = [
    ("image/png", png, 640, 480), ("image/jpeg", jpeg, 1024, 768), ("image/gif", gif, 320, 200),
    ("image/webp", webp_vp8, 800, 600), ("image/webp", webp_vp8l, 123, 4567), ("image/webp", webp_vp8x, 5000, 3000),
]


def test_admit_each_type_with_dimensions():
    for mime, mk, w, h in CASES:
        r = ev(mime, mk(w, h))
        assert r["outcome"] == "admit" and r["reason"] is None, (mime, r)
        assert (r["width"], r["height"], r["media_type"]) == (w, h, mime)
        assert p.sniff(mk(w, h)) == (mime, w, h)


def test_mime_normalisation():
    assert ev("image/jpg; charset=x", jpeg(10, 10))["outcome"] == "admit"
    assert ev("IMAGE/PNG", png(10, 10))["outcome"] == "admit"


def test_max_bytes_parameter():
    big = p.MAX_BYTES + 1
    assert ev("image/png", png(1, 1), size=big)["reason"] == "too_large_bytes"
    r = ev("image/png", png(1, 1), size=big, max_bytes=big)
    assert r["outcome"] == "admit" and r["limits"]["max_bytes"] == big
    assert ev("image/png", png(1, 1), size=11, max_bytes=10)["reason"] == "too_large_bytes"
    assert ev("image/png", png(1, 1))["limits"]["max_bytes"] == p.MAX_BYTES


def test_over_byte_cap():
    assert ev("image/png", png(1, 1), size=p.MAX_BYTES)["outcome"] == "admit"
    assert ev("image/png", png(1, 1), size=p.MAX_BYTES + 1)["reason"] == "too_large_bytes"


def test_pixel_cap_without_decoding():
    pil_before = "PIL" in sys.modules      # another test module may have imported Pillow; evaluate() itself must not
    r = ev("image/png", png(30000, 30000))
    assert r["outcome"] == "omit" and r["reason"] == "too_many_pixels"
    assert (r["width"], r["height"]) == (30000, 30000)
    assert ev("image/jpeg", jpeg(30000, 30000))["reason"] == "too_many_pixels"
    assert ev("image/png", png(9460, 9459))["reason"] == "too_many_pixels"
    assert ev("image/png", png(9459, 9459))["outcome"] == "admit"  # 89_472_681 <= cap
    assert ("PIL" in sys.modules) == pil_before


def test_side_too_long():
    assert ev("image/webp", webp_vp8x(70000, 10))["reason"] == "side_too_long"
    assert ev("image/png", png(65535, 1))["outcome"] == "admit"
    assert ev("image/png", png(65536, 1))["reason"] == "side_too_long"


def test_unsupported_type():
    for m in ("video/mp4", "application/pdf", "", "image/svg+xml", "image/bmp"):
        assert ev(m, b"\x00\x00\x00\x18ftypmp42")["reason"] == "unsupported_type", m
    assert ev(None, b"")["reason"] == "unsupported_type"


def test_type_mismatch():
    assert ev("image/png", jpeg(10, 10))["reason"] == "type_mismatch"
    assert ev("image/gif", png(10, 10))["reason"] == "type_mismatch"
    assert ev("image/jpeg", webp_vp8(10, 10))["reason"] == "type_mismatch"


def test_truncated_and_malformed_headers():
    bad = [
        ("image/png", png(5, 5)[:20]), ("image/png", png(5, 5)[:8]),
        ("image/png", png(5, 5).replace(b"IHDR", b"IDAT")), ("image/png", png(0, 5)),
        ("image/gif", gif(5, 5)[:8]), ("image/gif", gif(0, 5)),
        ("image/jpeg", b"\xff\xd8"), ("image/jpeg", jpeg(5, 5)[:12]), ("image/jpeg", jpeg(5, 5)[:-3]),
        ("image/jpeg", b"\xff\xd8\x00\x00\x00\x00"),
        ("image/jpeg", b"\xff\xd8\xff\xe0\xff\xff" + b"\x00" * 10),  # segment length past buffer
        ("image/jpeg", b"\xff\xd8\xff\xe0\x00\x00\x00\x00"),  # length < 2
        ("image/jpeg", b"\xff\xd8\xff\xda\x00\x08" + b"\x00" * 8),  # SOS before SOF
        ("image/jpeg", b"\xff\xd8" + b"\xff\xe1\x00\x02" * 5000),  # long loop must terminate
        ("image/jpeg", b"\xff\xd8" + b"\xff" * 70000),  # fill bytes only
        ("image/webp", webp_vp8(5, 5)[:25]), ("image/webp", webp_vp8l(5, 5)[:22]),
        ("image/webp", webp_vp8x(5, 5)[:28]), ("image/webp", riff(b"JUNK" + b"\x00" * 30)),
        ("image/webp", webp_vp8(5, 5).replace(b"\x9d\x01\x2a", b"\x00\x00\x00")),
        ("image/webp", webp_vp8l(5, 5).replace(b"\x2f", b"\x00", 1)),
        ("image/png", b""), ("image/png", b"not an image at all"),
    ]
    for mime, head in bad:
        r = ev(mime, head)
        assert r["outcome"] == "omit" and r["reason"] == "unreadable_header", (mime, head[:12], r)
        assert r["width"] is None and r["height"] is None
    assert ev("image/png", "text")["reason"] == "unreadable_header"  # wrong head type: no exception


def test_head_is_bounded():
    far = b"\xff\xd8" + b"\xff\xe1\xff\xfd" + b"\x00" * 65531 + jpeg(5, 5)[2:]
    assert ev("image/jpeg", far)["reason"] == "unreadable_header"


def test_route_without_vision():
    assert ev("image/png", png(5, 5), vision=False)["reason"] == "route_without_vision"
    assert ev("image/png", png(5, 5), vision=None)["reason"] == "route_without_vision"


def test_receipt_shape_and_privacy():
    for r in (ev("image/png", png(5, 5)), ev("video/mp4", b""), ev("image/png", b"x"), ev("../../etc/passwd", b"")):
        assert set(r) == KEYS
        assert r["policy"] == "media-input-v1"
        assert set(r["limits"]) == {"max_bytes", "max_pixels", "max_side"}
        assert r["reason"] is None or r["reason"] in p.REASONS
        assert "passwd" not in repr(r) and "/" not in (r["media_type"] or "/").replace("/", "", 1)
    assert ev("image/png", png(5, 5)) == ev("image/png", png(5, 5))


def test_stdlib_only():
    tree = ast.parse(pathlib.Path(p.__file__).read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            assert n.level == 0
            mods.add(n.module.split(".")[0])
    assert mods <= set(sys.stdlib_module_names), mods
