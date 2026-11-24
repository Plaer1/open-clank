import asyncio
import base64
import hashlib
import io
import os
import struct
import tempfile
from types import SimpleNamespace
from unittest import mock

# 1x1 RGB PNG, decodable by Pillow.
PNG1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
)
# Header-only claim of 30000x30000 (9e8 pixels) followed by padding; not a decodable image.
BOMB = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 30000, 30000)
        + b"\x08\x06\x00\x00\x00" + b"\x00" * 300_000)
# Values captured by running _normal() against the ORIGINAL (pre-edit) project files.
EXPECTED_NORMAL = {
    "content": [{"text": "look", "type": "text"}, {"image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"}, "type": "image_url"}],
    "describe": ("described", "image/png", True),
    "stored_equal": True,
    "upload": {"checksum_sha256": "b1ff9c8ea3a780bad09b346c423d2d0e46815926879b18e841d928376a946640", "client_ip": "10.0.0.1",
               "hash": "b1ff9c8ea3a780bad09b346c423d2d0e46815926879b18e841d928376a946640", "height": 1, "mime": "image/png",
               "name": "p.png", "original_name": "p.png", "owner": "o", "size": 69, "width": 1},
}


class _Decoded(BaseException):
    """BaseException so save_upload's `except Exception` cannot swallow a decode attempt."""


def _handler():
    from src.upload_handler import UploadHandler
    root = tempfile.mkdtemp()
    os.mkdir(os.path.join(root, "b"))
    os.mkdir(os.path.join(root, "u"))
    return UploadHandler(base_dir=os.path.join(root, "b"), upload_dir=os.path.join(root, "u"))


def _save(h, data, name="p.png"):
    return h.save_upload(SimpleNamespace(file=io.BytesIO(data), filename=name), "10.0.0.1", owner="o")


class _Stub:
    def __init__(self, uploads, cap=None):
        self.uploads = uploads
        if cap is not None:
            self.max_upload_size = cap

    def resolve_upload(self, fid, owner=None):
        return self.uploads.get(fid)

    def _inside_upload_dir(self, path):
        return True

    def is_image_file(self, name, mime):
        return (mime or "").startswith("image/")

    def is_audio_file(self, name, mime):
        return False

    def is_document_file(self, name, mime):
        return False


def _file(data, name="a.png"):
    p = os.path.join(tempfile.mkdtemp(), name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def _content(data, cap=None, **kw):
    import src.document_processor as dp
    p = _file(data)
    stub = _Stub({"i": {"path": p, "name": "a.png", "mime": "image/png"}}, cap)
    return dp.build_user_content("look", ["i"], os.path.dirname(p), stub, owner="o", **kw)


def _describe(path):
    from src.openclank import modality_facade as mf
    seen = {}

    async def fake(**kw):
        seen.update(media_type=kw["media_type"], image=kw["image"])
        return "described"

    with mock.patch.object(mf, "describe_image", fake):
        out = asyncio.run(mf.describe_image_path(owner="o", image_path=path))
    return out, seen


def _both(fn):
    p1, p2 = mock.patch("builtins.open", fn), mock.patch("io.open", fn)

    class _P:
        def __enter__(self):
            p1.__enter__(), p2.__enter__()

        def __exit__(self, *a):
            p2.__exit__(*a), p1.__exit__(*a)

    return _P()


def _spy_reads(target):
    """Context manager patching builtins.open; returns the list of read() sizes on target."""
    reads = []
    real = open

    class Spy:
        def __init__(self, f):
            self._f = f

        def read(self, n=-1):
            d = self._f.read(n)
            reads.append(len(d))
            return d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()

        def __getattr__(self, a):
            return getattr(self._f, a)

    def spy_open(path, mode="r", *a, **k):
        f = real(path, mode, *a, **k)
        return Spy(f) if str(path) == target and "b" in mode else f

    # pathlib opens through io.open, so patch both names.
    return reads, _both(spy_open)


def _imgs(content):
    return [] if isinstance(content, str) else [b for b in content if b.get("type") == "image_url"]


def _text(content):
    return content if isinstance(content, str) else content[0]["text"]


def _normal():
    h = _handler()
    meta = _save(h, PNG1)
    with open(meta["path"], "rb") as f:
        stored = f.read()
    volatile = {"id", "path", "uploaded_at", "created_at", "last_accessed", "media_input"}   # the receipt is asserted on its own below
    up = {k: v for k, v in meta.items() if k not in volatile}
    content = _content(PNG1)
    out, seen = _describe(_file(PNG1))
    return {
        "upload": up, "stored_equal": stored == PNG1, "content": content,
        "describe": (out, seen["media_type"], seen["image"] == PNG1),
    }


def test_normal_image_unchanged_at_each_site():
    got = _normal()
    assert got["stored_equal"]
    assert got["upload"]["width"] == 1 and got["upload"]["height"] == 1
    admitted = _save(_handler(), PNG1)["media_input"]    # BQ-21: the receipt of every evaluated image is kept, sent or omitted
    assert admitted["outcome"] == "admit" and admitted["reason"] is None and (admitted["width"], admitted["height"]) == (1, 1)
    assert got["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG1).decode()}}
    assert got["describe"] == ("described", "image/png", True)
    assert got == EXPECTED_NORMAL, got


def test_bomb_never_decoded_at_upload_and_stored_unchanged():
    h = _handler()
    with mock.patch("PIL.Image.open", side_effect=_Decoded):
        meta = _save(h, BOMB)
    with open(meta["path"], "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == hashlib.sha256(BOMB).hexdigest() == meta["hash"]
    assert "width" not in meta and "height" not in meta
    assert meta["media_input"]["outcome"] == "omit" and meta["media_input"]["reason"] == "too_many_pixels"
    # admit path still decodes exactly once
    with mock.patch("PIL.Image.open", wraps=__import__("PIL.Image", fromlist=["open"]).open) as spy:
        meta = _save(_handler(), PNG1)
    assert spy.call_count == 1 and meta["width"] == 1


def test_bomb_omitted_at_model_request_without_full_read():
    p = _file(BOMB)
    stub = _Stub({"i": {"path": p, "name": "a.png", "mime": "image/png"}})
    import src.document_processor as dp
    reads, patch = _spy_reads(p)
    with patch:
        content = dp.build_user_content("look", ["i"], os.path.dirname(p), stub, owner="o")
    assert not _imgs(content)
    assert "[Image not sent: a.png" in _text(content) and "too many pixels" in _text(content) and os.path.dirname(p) not in _text(content)
    assert reads and max(reads) <= 65536 and sum(reads) < len(BOMB)


def test_route_without_vision_yields_note_not_image():
    content = _content(PNG1, route_has_vision=False)
    assert not _imgs(content)
    assert _text(content) == "look"  # no second note: chat_handler already adds the no-vision one
    # omitted parameter keeps today's behaviour for any other caller
    assert _imgs(_content(PNG1))


def test_effective_cap_is_honoured():
    data = PNG1 + b"\x00" * 3000  # valid header, 3 KB on disk
    assert not _imgs(_content(data, cap=1000))
    assert "larger than the allowed size" in _text(_content(data, cap=1000))
    assert _imgs(_content(data, cap=5000))


def test_describe_image_path_omits_without_full_read():
    from src.openclank import modality_facade as mf
    from src.openclank.operation_router import ManagedOperationUnavailable
    p = _file(BOMB)
    reads, patch = _spy_reads(p)
    called = []

    async def fake(**kw):
        called.append(1)

    with patch, mock.patch.object(mf, "describe_image", fake):
        try:
            asyncio.run(mf.describe_image_path(owner="o", image_path=p))
            raise AssertionError("expected ManagedOperationUnavailable")
        except ManagedOperationUnavailable as exc:
            assert "too_many_pixels" in str(exc) and p not in str(exc)
    assert not called and reads and max(reads) <= 65536 and sum(reads) < len(BOMB)


def test_chat_handler_threads_vision_flag():
    import src.chat_handler as ch
    assert "route_has_vision=main_is_vision or not (allow_tool_preprocessing and vision_enabled)" in open(ch.__file__).read()


def _bmp(w, h, pixels=True):
    row = b"\x00\x00\xff\x00" if pixels else b""
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(row), 2835, 2835, 0, 0)
    return b"BM" + struct.pack("<IHHI", 54 + len(row), 0, 0, 54) + dib + row


def test_fake_and_mislabelled_images_still_sent_at_model_request():
    fake = b"\x89PNG\r\n\x1a\nfake"
    got = _imgs(_content(fake))
    assert got and got[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(fake).decode()
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, "JPEG")
    got = _imgs(_content(buf.getvalue()))  # real JPEG stored under image/png
    assert got and got[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_bmp_dimensions_kept_and_bmp_bomb_not_decoded():
    meta = _save(_handler(), _bmp(1, 1), "b.bmp")
    assert meta["mime"] == "image/bmp" and (meta["width"], meta["height"]) == (1, 1)
    assert meta["media_input"]["outcome"] == "admit"   # BQ-21: a receipt is kept for every input; Pillow read this BMP, so the decode guard admitted it
    from PIL import Image
    bomb = _bmp(30000, 30000, pixels=False)
    with mock.patch("PIL.ImageOps.exif_transpose", side_effect=_Decoded), \
         mock.patch.object(Image.Image, "load", side_effect=_Decoded):
        meta = _save(_handler(), bomb, "b.bmp")
    with open(meta["path"], "rb") as f:
        assert f.read() == bomb
    assert "width" not in meta and meta["media_input"]["reason"] == "too_many_pixels"
    # a bomb declared by Pillow's header (under Pillow's own 2x refusal) is caught by our check
    mid = _bmp(10000, 10000, pixels=False)  # 1e8 px: above MAX_PIXELS, below Pillow's refusal
    with mock.patch("PIL.ImageOps.exif_transpose", side_effect=_Decoded):
        meta = _save(_handler(), mid, "m.bmp")
    assert "width" not in meta and meta["media_input"]["reason"] == "too_many_pixels"


def test_describe_accepts_file_over_10_mib_but_under_native_limit():
    p = _file(PNG1)
    with open(p, "r+b") as f:
        f.truncate(11 * 1024 * 1024)  # sparse; valid small header
    out, seen = _describe(p)
    assert out == "described" and seen["media_type"] == "image/png"
    with open(p, "r+b") as f:
        f.truncate(151 * 1024 * 1024)
    from src.openclank.operation_router import ManagedOperationUnavailable
    try:
        _describe(p)
        raise AssertionError("expected omit")
    except ManagedOperationUnavailable as exc:
        assert "too_large_bytes" in str(exc)
    # header problems are not blocking there: fake bytes are still described
    assert _describe(_file(b"\x89PNG\r\n\x1a\nfake"))[0] == "described"
