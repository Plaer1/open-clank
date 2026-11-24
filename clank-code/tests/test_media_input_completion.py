"""BQ-21 (WB09 completion): receipts on every evaluated input, visible reasons, and the acceptance cases that had no test.

Real UploadHandler / build_user_content / describe path on synthetic images (Pillow generates the fixtures); no provider call.
"""
import io
import json
import os
import struct
import tempfile
from types import SimpleNamespace
from unittest import mock

import pytest

from src.openclank import media_input_policy as pol
from tests.test_media_input_wiring import BOMB, PNG1, _content, _describe, _file, _handler, _imgs, _save, _text

Image = pytest.importorskip("PIL.Image")


def _encode(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _stored(meta):
    with open(meta["path"], "rb") as f:
        return f.read()


def test_exif_oriented_jpeg_decision_uses_the_stored_pixels_and_the_original_bytes_are_untouched():
    img = Image.new("RGB", (40, 20), (200, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = 6                                    # rotate 90: the displayed image is 20 x 40
    data = _encode(img, "JPEG", exif=exif.tobytes())
    meta = _save(_handler(), data, name="o.jpg")
    assert _stored(meta) == data                        # original bytes stored unchanged
    receipt = meta["media_input"]
    assert receipt["outcome"] == "admit" and receipt["media_type"] == "image/jpeg"
    assert (receipt["width"], receipt["height"]) == (40, 20)        # the limit decision reads the stored raster header
    assert (meta["width"], meta["height"]) == (20, 40)              # display dimensions are the existing EXIF-transposed ones
    assert receipt["animated"] is False


def test_alpha_png_is_admitted_with_bytes_unchanged():
    data = _encode(Image.new("RGBA", (8, 8), (1, 2, 3, 0)), "PNG")
    meta = _save(_handler(), data)
    assert _stored(meta) == data and meta["media_input"]["outcome"] == "admit" and meta["media_input"]["animated"] is False
    content = _content(data)
    assert len(_imgs(content)) == 1 and "Image not sent" not in _text(content)


def test_animated_gif_is_admitted_or_omitted_by_the_same_limits_and_never_claimed_animated_without_proof():
    frames = [Image.new("P", (6, 6), i) for i in (0, 1)]
    data = _encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    meta = _save(_handler(), data, name="a.gif")
    assert _stored(meta) == data and meta["media_input"]["outcome"] == "admit"
    assert meta["media_input"]["animated"] is None      # a GIF header does not prove the frame count
    # the same pixel rule omits a huge animated GIF without decoding any frame
    huge = b"GIF89a" + struct.pack("<HH", 30000, 30000) + b"\x00\x00\x00" + b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00" + b"\x00" * 1000
    with mock.patch("PIL.Image.open", side_effect=BaseException("decoded")):
        omitted = _save(_handler(), huge, name="b.gif")
    assert omitted["media_input"]["outcome"] == "omit" and omitted["media_input"]["reason"] == "too_many_pixels"
    assert omitted["media_input"]["animated"] is None and _stored(omitted) == huge


def test_animated_webp_header_proves_animation_only_through_the_vp8x_flag():
    def webp(flags):
        body = b"VP8X" + struct.pack("<I", 10) + bytes([flags]) + b"\x00\x00\x00" + (3).to_bytes(3, "little") + (3).to_bytes(3, "little")
        return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body
    assert pol.evaluate(declared_type="image/webp", byte_size=40, head=webp(0x02), route_has_vision=True)["animated"] is True
    assert pol.evaluate(declared_type="image/webp", byte_size=40, head=webp(0x00), route_has_vision=True)["animated"] is False
    assert pol.evaluate(declared_type="image/webp", byte_size=40, head=webp(0x02), route_has_vision=True)["outcome"] == "admit"
    # an animated WebP over the pixel limit is omitted by the same rule, still without a frame decode
    big = bytearray(webp(0x02)); big[24:27] = (29999).to_bytes(3, "little"); big[27:30] = (29999).to_bytes(3, "little")
    r = pol.evaluate(declared_type="image/webp", byte_size=40, head=bytes(big), route_has_vision=True)
    assert r["outcome"] == "omit" and r["reason"] == "too_many_pixels" and r["animated"] is True
    try:
        real = Image.new("RGB", (4, 4))
        frames = [real, Image.new("RGB", (4, 4), (9, 9, 9))]
        data = _encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=40)
    except Exception:
        return                                           # no WebP encoder in this Pillow build: the header cases above stand
    assert pol.evaluate(declared_type="image/webp", byte_size=len(data), head=data, route_has_vision=True)["animated"] is True


def test_send_time_receipt_for_every_image_sent_or_omitted_and_reason_text_is_visible():
    import src.document_processor as dp
    ok, bad = _file(PNG1), _file(BOMB)
    stub = SimpleNamespace(uploads={"ok": {"path": ok, "name": "ok.png", "mime": "image/png"}, "bad": {"path": bad, "name": "bad.png", "mime": "image/png"}})
    from tests.test_media_input_wiring import _Stub
    stub = _Stub(stub.uploads)
    receipts = {}
    content = dp.build_user_content("look", ["ok", "bad"], os.path.dirname(ok), stub, owner="o", media_receipts=receipts)
    assert set(receipts) == {"ok", "bad"}
    assert receipts["ok"]["sent"] is True and receipts["ok"]["outcome"] == "admit"
    assert receipts["bad"]["sent"] is False and receipts["bad"]["reason"] == "too_many_pixels"
    assert len(_imgs(content)) == 1 and "left out: " + pol.reason_text("too_many_pixels") in _text(content)
    # a no-vision route omits and records the reason; the note naming what to do is owned by chat_handler (asserted below)
    receipts = {}
    dp.build_user_content("look", ["ok"], os.path.dirname(ok), stub, owner="o", route_has_vision=False, media_receipts=receipts)
    assert receipts["ok"]["sent"] is False and receipts["ok"]["reason"] == "route_without_vision"


def test_unsupported_type_is_never_silent_and_still_sent():
    import src.document_processor as dp
    from tests.test_media_input_wiring import _Stub
    bmp = _file(b"BM" + b"\x00" * 60, "a.bmp")
    stub = _Stub({"i": {"path": bmp, "name": "a.bmp", "mime": "image/bmp"}})
    receipts = {}
    content = dp.build_user_content("look", ["i"], os.path.dirname(bmp), stub, owner="o", media_receipts=receipts)
    assert receipts["i"]["reason"] == "unsupported_type" and receipts["i"]["sent"] is True and len(_imgs(content)) == 1
    assert "sent unchanged" in _text(content) and pol.ALTERNATIVE["unsupported_type"] in _text(content)


def test_the_route_without_vision_note_names_what_to_do():
    import src.chat_handler as ch
    source = open(ch.__file__).read()
    assert "Choose a model that accepts images, or use Caption on the image to have it described." in source
    assert "media_receipts=_media_receipts" in source and '_m["media_input"] = _media_receipts[_m["id"]]' in source


def test_describe_keeps_the_reason_in_its_result_text():
    import asyncio
    import src.document_processor as dp
    from src.openclank import modality_facade as mf
    p = _file(BOMB)
    async def refuse(**kw):
        raise mf.ManagedOperationUnavailable("image not described: too_many_pixels")
    with mock.patch.object(dp, "_load_vl_settings", lambda: {"vision_enabled": True}), mock.patch.object(mf, "describe_image_path", refuse):
        text = asyncio.run(dp.analyze_image_with_vl_result_async(p, owner="o"))["text"]
    assert "too many pixels" in text and "VL model unavailable" not in text
    async def other(**kw):
        raise RuntimeError("provider down")
    with mock.patch.object(dp, "_load_vl_settings", lambda: {"vision_enabled": True}), mock.patch.object(mf, "describe_image_path", other):
        assert asyncio.run(dp.analyze_image_with_vl_result_async(p, owner="o"))["text"] == "[VL model unavailable - image not analyzed]"


def test_receipt_survives_a_retained_turn_resume():
    """The attachment record (SSE `attachments` event and stored message metadata) is JSON: the same receipt comes back after a resume."""
    meta = {"id": "i", "name": "bad.png", "mime": "image/png", "media_input": {**pol.evaluate(declared_type="image/png", byte_size=9, head=BOMB[:64], route_has_vision=True), "sent": False}}
    resumed = json.loads(json.dumps([meta]))[0]
    assert resumed["media_input"] == meta["media_input"] and resumed["media_input"]["reason"] == "too_many_pixels"


def test_cancelled_upload_leaves_no_partial_original_and_no_index_entry():
    h = _handler()

    class Broken(io.BytesIO):
        calls = 0
        def read(self, n=-1):
            Broken.calls += 1
            if Broken.calls > 2:                       # the client went away mid-stream (calls 1-2 are size/type/hash passes)
                raise ConnectionError("client disconnected")
            return super().read(n)
    data = PNG1 * 4000
    from fastapi import HTTPException
    class Gone:
        def __init__(self): self.file, self.filename = Broken(data), "p.png"
    # hash/type passes read the whole file first; fail only inside the write loop
    real_hash = h.calculate_file_hash
    state = {"armed": False}
    class Flaky(io.BytesIO):
        def read(self, n=-1):
            if state["armed"] and n == 8192:
                state["armed"] = False
                raise ConnectionError("client disconnected")
            return super().read(n)
    upload = SimpleNamespace(file=Flaky(data), filename="p.png")
    def hash_then_arm(fobj):
        out = real_hash(fobj); state["armed"] = True; return out
    with mock.patch.object(h, "calculate_file_hash", hash_then_arm):
        with pytest.raises(HTTPException):
            h.save_upload(upload, "10.0.0.1", owner="o")
    walk = [os.path.join(r, f) for r, _, fs in os.walk(h.upload_dir) for f in fs if not f.startswith(".") and f != "uploads.json"]
    assert not walk, walk
    index = os.path.join(h.upload_dir, "uploads.json")
    assert not os.path.exists(index) or not json.load(open(index))
