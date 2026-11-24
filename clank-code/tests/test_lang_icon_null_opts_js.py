"""Pin the code-native language and filesystem glyph helpers.
Driven through `node --input-type=module`; skips without node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "langIcons.js"
_HAS_NODE = shutil.which("node") is not None


def _icon(lang, size, opts):
    js = f"""
    import {{ langIcon }} from '{_HELPER.as_posix()}';
    console.log(langIcon({json.dumps(lang)}, {json.dumps(size)}, {json.dumps(opts)}));
    """
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _file_glyphs(descriptors):
    js = f"""
    import {{ fileIcon, fileIconKey }} from '{_HELPER.as_posix()}';
    const descriptors = {json.dumps(descriptors)};
    console.log(JSON.stringify(descriptors.map((descriptor) => ({{
      key: fileIconKey(descriptor),
      markup: fileIcon(descriptor, 18, null),
    }}))));
    """
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js, capture_output=True, text=True, cwd=str(_REPO), timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_lang_icon_tolerates_null_opts():
    # `opts = {}` default only applies when the arg is omitted; an explicit
    # null (easy to pass) hit opts.className and threw a TypeError.
    out = _icon("python", 14, None)
    assert out.startswith("<svg")
    assert "class=" not in out


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_lang_icon_applies_opts_when_given():
    assert 'class="ic"' in _icon("python", 14, {"className": "ic"})


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_file_icon_key_normalizes_authorized_metadata_without_content_reads():
    cases = [
        ({"name": "misleading.png", "kind": "Directory"}, "folder"),
        ({"name": "folder", "kind": "directory", "open": True}, "folder-open"),
        ({"filename": "PHOTO.PNG", "type": "File"}, "image"),
        ({"path": "/tmp/backup.TAR.GZ", "kind": "File"}, "archive"),
        ({"name": "report", "media_type": "application/pdf; charset=binary"}, "pdf"),
        ({"name": "data.csv", "kind": "File"}, "csv"),
        ({"name": "app.JSX", "kind": "File"}, "javascript"),
        ({"name": "view.TSX", "kind": "File"}, "typescript"),
        ({"name": "Program.cs", "kind": "File"}, "csharp"),
        ({"name": "link", "kind": "Symlink"}, "symlink"),
        ({"name": "Untitled", "language": " Markdown "}, "markdown"),
        ({"name": "Untitled", "media_type": "markdown"}, "markdown"),
        ({"name": "Untitled", "media_type": "image"}, "image"),
        ({"name": "Untitled", "mimeType": "application/json"}, "json"),
        ({"name": "Untitled", "mimeType": "image/svg+xml"}, "svg"),
        ({"name": "Untitled", "mime_type": "text/csv"}, "csv"),
        ({"name": "wait", "state": "pending"}, "loading"),
        ({"name": "gone", "icon_state": "offline"}, "unavailable"),
        ({"navigation_role": "favorites"}, "star-filled"),
        (None, "file"),
    ]
    rendered = _file_glyphs([descriptor for descriptor, _ in cases])
    assert [item["key"] for item in rendered] == [expected for _, expected in cases]


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_file_icons_are_nonempty_theme_safe_decorative_svg_fallbacks():
    descriptors = [
        {"name": "folder", "kind": "directory"},
        {"name": "photo.png", "kind": "file"},
        {"name": "bundle.zip", "kind": "file"},
        {"name": "README.md", "kind": "file"},
        {"name": "<script>alert(1)</script>.unknown", "kind": "other"},
    ]
    rendered = _file_glyphs(descriptors)
    for item in rendered:
        markup = item["markup"]
        assert markup.startswith("<svg")
        assert 'viewBox="0 0 24 24"' in markup
        assert 'stroke="currentColor"' in markup
        assert 'aria-hidden="true"' in markup
        assert 'focusable="false"' in markup
        assert "<script>" not in markup
    assert rendered[0]["markup"] != rendered[-1]["markup"]
    assert rendered[1]["markup"] != rendered[-1]["markup"]
