"""Theme hex colors expand shorthand and reject invalid input."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_theme_hex_colors():
    script = r"""
    import assert from 'node:assert/strict';
    import { hexToRgb } from './static/js/color/hex.js';

    const cases = [
      ["#abc", {"r": 170, "g": 187, "b": 204}],
      ["#ff8800", {"r": 255, "g": 136, "b": 0}],
      ["ff8800", {"r": 255, "g": 136, "b": 0}],
      ["nothex", null],
      ["", null],
    ];
    for (const [input, expected] of cases) {
      assert.deepEqual(hexToRgb(input), expected, JSON.stringify(input));
    }
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
