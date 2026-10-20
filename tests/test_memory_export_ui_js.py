"""S01 — Memory export controls UI (EC-D04/EC-D05) contract tests.

Behavioral coverage runs in Node against the real modules
(tests/js/test_memory_export.mjs + tests/js/test_memory_export_dialog.mjs):
query-string assembly mirrors `_parse_export_filters`, the export dialog
renders/downloads against a mocked fetch, and per-photo download toasts
typed errors. Skipped when node is unavailable, mirroring
tests/test_streaming_segmenter_js.py.

The DOM-coupled wiring in static/js/memory.js (button → dialog, photo cards
in the import batch review) is pinned here with source anchors, matching
tests/test_memory_brain_ui_js.py's idiom for browser-coupled code.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None

needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


@needs_node
def test_memory_export_ui_suite():
    test_files = [
        _REPO / "tests" / "js" / "test_memory_export.mjs",
        _REPO / "tests" / "js" / "test_memory_export_dialog.mjs",
    ]
    result = subprocess.run(
        ["node", "--test", *(str(p) for p in test_files)],
        cwd=_REPO,
        capture_output=True,
        timeout=180,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node --test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def test_export_button_opens_dialog_and_default_path_is_unchanged():
    source = (_REPO / "static" / "js" / "memory.js").read_text()
    assert "createMemoryExportDialog" in source
    assert "export function exportMemories()" in source
    assert "_memoryExportDialog.open()" in source
    # The dialog module, not the button handler, owns the fetch — the
    # untouched-default query must stay exactly '?format=bundle'.
    dialog = (_REPO / "static" / "js" / "memoryExportDialog.js").read_text()
    assert "'Exported the canonical Memory bundle'" in dialog
    util = (_REPO / "static" / "js" / "util" / "memoryExport.js").read_text()
    assert "MEMORY_EXPORT_FILTER_INVALID" in util
    assert "assets_only requires format=bundle." in util


def test_photo_review_cards_carry_per_photo_download():
    source = (_REPO / "static" / "js" / "memory.js").read_text()
    assert "_batchPhotoItems" in source
    assert "item?.result?.media?.asset_id" in source
    assert "memoryAssetUrl(photo.assetId)" in source
    assert "downloadMemoryPhoto(photo.assetId, photo.filename)" in source
    dialog = (_REPO / "static" / "js" / "memoryExportDialog.js").read_text()
    assert "export async function downloadMemoryAsset" in dialog
    assert "memoryAssetUrl(assetId)" in dialog
    assert "'Photo download failed'" in dialog
    util = (_REPO / "static" / "js" / "util" / "memoryExport.js").read_text()
    assert "/api/memory/assets/${encodeURIComponent(id)}" in util
