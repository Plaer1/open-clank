import base64
import json
import subprocess
from pathlib import Path

from src.document_processor import build_user_content
from src.openclank.acp_bridge import _content_parts


class _Handler:
    def __init__(self, uploads):
        self.uploads = uploads

    def resolve_upload(self, fid, owner=None):
        return self.uploads.get(fid)

    def _inside_upload_dir(self, path):
        return True

    def is_image_file(self, name, mime):
        return mime.startswith("image/")

    def is_audio_file(self, name, mime):
        return mime.startswith("audio/")

    def is_document_file(self, name, mime):
        return False


def _native_transform(messages):
    root = Path(__file__).resolve().parents[1]
    script = r'''
import { ProviderTransform } from "./src/provider"
import { ProviderTest } from "./test/fake/provider"
const input = JSON.parse(await new Response(Bun.stdin.stream()).text())
const model = ProviderTest.model({
  api: { id: "fixture", url: "https://wire.invalid", npm: "@ai-sdk/openai-compatible" },
  capabilities: { ...ProviderTest.model().capabilities, input: { text: true, image: true, audio: true, video: true, pdf: true } },
})
const output = ProviderTransform.message(input, model, {})
console.log(JSON.stringify(output.map((m) => ({ ...m, content: Array.isArray(m.content) ? m.content.map((p) => ({ type: p.type, data: typeof p.data === "string" ? p.data : undefined, image: typeof p.image === "string" ? p.image : undefined, mediaType: p.mediaType, text: p.text })) : m.content }))))
'''
    result = subprocess.run(
        ["bun", "-e", script], cwd=root / "packages/mimo-code/packages/opencode",
        input=json.dumps(messages), text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def test_build_to_acp_to_native_preserves_fresh_and_restored_bytes(tmp_path):
    image = tmp_path / "photo.png"
    audio = tmp_path / "sound.ogg"
    image.write_bytes(b"image-original")
    audio.write_bytes(b"audio-original")
    uploads = {
        "image": {"path": str(image), "name": "photo.png", "mime": "image/png"},
        "audio": {"path": str(audio), "name": "sound.ogg", "mime": "audio/ogg"},
    }
    handler = _Handler(uploads)
    fresh = build_user_content("inspect", ["image", "audio"], str(tmp_path), handler, structured_resources=True)
    restored = build_user_content("inspect", ["image", "audio"], str(tmp_path), handler, structured_resources=True)
    fresh_parts = _content_parts(fresh, attachment_names=["photo.png", "sound.ogg"])
    restored_parts = _content_parts(restored, attachment_names=["photo.png", "sound.ogg"])
    assert fresh_parts == restored_parts
    messages = [{"role": "user", "content": [
        {"type": "file", "data": f"data:{part['mimeType']};base64,{part['data']}", "mediaType": part["mimeType"]}
        for part in fresh_parts if part.get("type") == "image"
    ] + [{"type": "file", "data": f"data:{part['resource']['mimeType']};base64,{part['resource']['blob']}", "mediaType": part["resource"]["mimeType"]}
        for part in fresh_parts if part.get("type") == "resource"]}]
    messages[0]["content"].extend([
        {"type": "file", "data": "data:video/mp4;base64," + base64.b64encode(b"video-original").decode(), "mediaType": "video/mp4"},
        {"type": "file", "data": "data:application/pdf;base64," + base64.b64encode(b"pdf-original").decode(), "mediaType": "application/pdf"},
    ])
    transformed = _native_transform(messages)
    output_parts = transformed[0]["content"]
    assert [part.get("mediaType") for part in output_parts[:3]] == ["image/png", "audio/ogg", "video/mp4"]
    assert [part.get("data") or part.get("image") for part in output_parts[:2]] == [
        f"data:image/png;base64,{base64.b64encode(b'image-original').decode()}",
        f"data:audio/ogg;base64,{base64.b64encode(b'audio-original').decode()}",
    ]
    assert output_parts[2]["data"] == "data:video/mp4;base64," + base64.b64encode(b"video-original").decode()
    assert output_parts[3]["type"] == "text"
    assert "pdf" in output_parts[3]["text"].lower()
