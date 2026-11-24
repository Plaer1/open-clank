"""Markdown rows preserve empty cells and tolerate malformed input."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_markdown_table_cells():
    script = r"""
    import assert from 'node:assert/strict';
    import { splitTableRow } from './static/js/markdown/tableRow.js';

    const cases = [
      ["| a |  | c |", ["a", "", "c"]],
      ["a | b | c", ["a", "b", "c"]],
      ["| h1 | h2 | h3 |", ["h1", "h2", "h3"]],
      [null, [""]],
      [{"bad": "row"}, [""]],
    ];
    for (const [input, expected] of cases) {
      assert.deepEqual(splitTableRow(input), expected, JSON.stringify(input));
    }
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
