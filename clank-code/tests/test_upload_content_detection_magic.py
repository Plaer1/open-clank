"""Content detection prefers file bytes to a misleading extension."""
import io

import pytest

from src.upload_handler import UploadHandler

# 1x1 PNG (header is enough for libmagic to report image/png).
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

def test_content_detection_overrides_misleading_extension(tmp_path):
    handler = UploadHandler(base_dir=str(tmp_path), upload_dir=str(tmp_path))
    if handler.file_detector is None:
        pytest.skip("libmagic/python-magic not installed in this environment")

    # PNG bytes behind a .bin name: extension sniffing can't help, so a correct
    # image/png result proves content-based detection is doing the work.
    detected = handler.detect_content_type(io.BytesIO(_PNG), "payload.bin")
    assert detected == "image/png"
