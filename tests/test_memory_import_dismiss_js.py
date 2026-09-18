"""S01 — Import-batch dismissal UI (RF-D03) contract tests.

Behavioral coverage runs in Node against the real module
(tests/js/test_memory_import_dismiss.mjs): the banner dismiss helper refreshes
server truth, POSTs every awaiting-review batch, and toasts typed errors while
keeping the banner visible. Skipped when node is unavailable,
mirroring tests/test_memory_export_ui_js.py.

The DOM-coupled wiring in static/js/memory.js (banner dismiss button →
helper) is pinned here with source anchors, matching
tests/test_memory_export_ui_js.py's idiom for browser-coupled code.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None

needs_node = pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")


@needs_node
def test_memory_import_dismiss_ui_suite():
    result = subprocess.run(
        ["node", "--test", str(_REPO / "tests" / "js" / "test_memory_import_dismiss.mjs")],
        cwd=_REPO,
        capture_output=True,
        timeout=180,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node --test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def test_banner_dismiss_posts_server_side_instead_of_hiding():
    source = (_REPO / "static" / "js" / "memory.js").read_text()
    assert "dismissAllPendingImportBatches" in source
    assert "origin: window.location.origin" in source
    helper = (_REPO / "static" / "js" / "memoryImportDismiss.js").read_text()
    assert "/api/memory/import-batches`" in helper
    assert "String(batch?.state) === 'awaiting_review'" in helper
    assert "/dismiss`" in helper
    assert "method: 'POST'" in helper
    assert "credentials: 'same-origin'" in helper
    assert "'Import dismiss failed'" in helper
