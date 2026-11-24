"""S01 — Import-batch dismissal UI (RF-D03) contract tests.

Behavioral coverage runs in Node against the real module
(tests/js/test_memory_import_dismiss.mjs): the banner dismiss helper refreshes
server truth, POSTs every awaiting-review batch, and toasts typed errors while
keeping the banner visible. Skipped when node is unavailable,
mirroring tests/test_memory_export_ui_js.py.

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
