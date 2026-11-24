from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_checked_in_supervisor_protocol_stubs_are_reproducible():
    result = subprocess.run(
        [sys.executable, "scripts/generate_agent_supervisor_protocol.py", "--check"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
