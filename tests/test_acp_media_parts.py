import base64

from src.openclank.acp_bridge import _content_parts, _data_uri


def test_acp_rejects_plain_data_and_mime_mismatch():
    assert _data_uri("data:image/png,plain") is None
    assert _data_uri("data:image/png;base64,") is None
    payload = base64.b64encode(b"image-bytes").decode()
    assert _data_uri(f"data:image/png;base64,{payload}", declared_mime="image/jpeg") is None


def test_acp_malformed_media_is_visible_instead_of_dropped():
    parts = _content_parts([
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,%%%"}},
        {"type": "input_audio", "input_audio": {"data": "%%%", "format": "ogg"}},
    ])
    assert len(parts) == 2
    assert all(part["type"] == "text" for part in parts)
    assert all("Attachment unavailable" in part["text"] for part in parts)


def test_acp_preserves_valid_media_bytes():
    payload = base64.b64encode(b"exact-bytes").decode()
    parts = _content_parts([
        {"type": "image", "url": f"data:image/png;base64,{payload}"},
        {"type": "input_audio", "input_audio": {"data": payload, "format": "ogg"}},
    ])
    assert parts[0]["data"] == payload
    assert parts[1]["resource"]["blob"] == payload


def test_acp_rejects_remote_and_unbounded_resource_payloads(tmp_path):
    parts = _content_parts([
        {"type": "image", "url": "https://example.test/image.png"},
        {"type": "resource", "resource": {"uri": "attachment://doc", "blob": "%%%"}},
        {"type": "resource", "resource": {"uri": "attachment://doc", "text": "x" * (150 * 1024 * 1024 + 1)}},
    ], workspace=str(tmp_path))
    assert len(parts) == 3
    assert all("Attachment unavailable" in part["text"] for part in parts)


def test_acp_resource_shape_and_remote_link_policy(tmp_path):
    parts = _content_parts([
        {"type": "resource", "resource": {"uri": "attachment://doc", "text": "ok", "blob": "%%%"}},
        {"type": "resource", "resource": {"uri": "attachment://doc", "blob": ""}},
        {"type": "resource_link", "uri": "https://example.test/a.pdf", "mimeType": "application/pdf"},
        {"type": "resource_link", "uri": "https://example.test/article", "mimeType": "text/html"},
    ], workspace=str(tmp_path))
    assert all("Attachment unavailable" in part["text"] for part in parts[:3])
    assert parts[3]["type"] == "resource_link"
