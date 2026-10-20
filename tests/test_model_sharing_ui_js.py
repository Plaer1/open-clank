import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def test_named_user_model_sharing_ui_contract():
    result = subprocess.run(
        ["node", str(ROOT / "tests/model_sharing_ui_contract.mjs")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "named-user model sharing UI contract checks passed" in result.stdout
