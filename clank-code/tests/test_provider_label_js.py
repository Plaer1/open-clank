"""Provider identity uses the host and model family. A port cannot identify the serving tool."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_provider_labels_and_logos():
    script = r"""
    import assert from 'node:assert/strict';
    import { providerLabel, providerLogo } from './static/js/providers.js';
    for (const [model, expected] of [
      ["mixtral-8x7b", true],
      ["ministral-8b", true],
      ["mistral-large-latest", true],
      ["totally-unknown-model-xyz", false],
    ]) {
      assert.equal(providerLogo(model) !== null, expected, model);
    }
    const cases = [
      ["http://localhost:8080/v1", "Local"],
      ["http://127.0.0.1:8080/v1", "Local"],
      ["http://localhost:8000/v1", "Local"],
      ["http://localhost:1234/v1", "Local"],
      ["http://localhost:11434/api", "Local"],
      ["http://localhost:9999/v1", "Local"],
      ["https://api.openai.com/v1", "OpenAI"],
      ["https://api.groq.com/openai/v1", "Groq"],
      ["http://192.168.1.50:8080", "Local"],
    ];
    for (const [input, expected] of cases) {
      assert.deepEqual(providerLabel(input), expected, JSON.stringify(input));
    }
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
