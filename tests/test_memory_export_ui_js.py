"""S01 — Memory export controls UI (EC-D04/EC-D05) contract tests.

Behavioral coverage runs in Node against the real modules
(tests/js/test_memory_export.mjs + tests/js/test_memory_export_dialog.mjs):
query-string assembly mirrors `_parse_export_filters`, the export dialog
renders/downloads against a mocked fetch, and per-photo download toasts
typed errors. Skipped when node is unavailable, mirroring
tests/test_streaming_segmenter_js.py.

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
