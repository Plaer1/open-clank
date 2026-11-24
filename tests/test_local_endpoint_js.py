"""Classify chat endpoints for local versus billable pricing."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_endpoint_billing_classification():
    script = r"""
    import assert from 'node:assert/strict';
    import { readFileSync } from 'node:fs';
    // chatRenderer requires a browser. Exercise its pure pricing classifier.
    const source = readFileSync('./static/js/chatRenderer.js', 'utf8');
    const definition = source.match(/export function isLocalEndpoint\(.*?\n\}/s);
    assert.ok(definition, 'isLocalEndpoint declaration');
    const isLocalEndpoint = new Function(definition[0].replace('export ', '') + '; return isLocalEndpoint;')();

    const cases = [
      ["http://llamaswap:8000", true],
      ["http://nim-nano:8000/v1", true],
      ["http://localhost:7000", true],
      ["http://127.0.0.1:11434", true],
      ["http://192.168.50.244", true],
      ["http://10.0.0.5:8080", true],
      ["http://172.16.0.9", true],
      ["http://server.local", true],
      ["https://api.openai.com/v1", false],
      ["https://openrouter.ai/api/v1", false],
      ["https://api.anthropic.com", false],
      ["https://generativelanguage.googleapis.com", false],
    ];
    for (const [input, expected] of cases) {
      assert.deepEqual(isLocalEndpoint(input), expected, JSON.stringify(input));
    }
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
