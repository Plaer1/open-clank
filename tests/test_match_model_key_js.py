"""Pricing lookup must prefer the longest matching model ID."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_model_pricing_matches():
    script = r"""
    import assert from 'node:assert/strict';
    import { matchModelKey } from './static/js/model/matchKey.js';
    const keys = ['gpt-4o', 'gpt-4o-mini', 'gpt-4', 'o1', 'o1-mini', 'o1-pro', 'o3', 'o3-mini'];
    const cases = [
      ["gpt-4o-mini", "gpt-4o-mini"],
      ["o1-mini", "o1-mini"],
      ["o1-pro", "o1-pro"],
      ["o3-mini", "o3-mini"],
      ["gpt-4o-2024-08-06", "gpt-4o"],
      ["some-unknown-model", null],
    ];
    for (const [input, expected] of cases) {
      assert.deepEqual(matchModelKey(input, keys), expected, JSON.stringify(input));
    }
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
