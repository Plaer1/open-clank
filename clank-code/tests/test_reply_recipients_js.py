"""Reply All excludes exact owned addresses and tolerates missing or malformed recipient fields."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_reply_all_cc():
    script = r"""
    import assert from 'node:assert/strict';
    import { buildReplyAllCc } from './static/js/emailLibrary/replyRecipients.js';

    const cases = [
      ['unknown sender', {to: 'Alice <alice@x.com>, bob@x.com', cc: 'Carol <carol@x.com>'}, '', 'Alice <alice@x.com>, bob@x.com, Carol <carol@x.com>'],
      ['exact sender', {to: 'Me <me@x.com>, Alice <alice@x.com>', cc: 'bob@x.com'}, 'me@x.com', 'Alice <alice@x.com>, bob@x.com'],
      ['multiple accounts', {to: 'Alice <alice@x.com>, me@work.com', cc: 'me@personal.com, bob@x.com'}, ['me@work.com', 'me@personal.com'], 'Alice <alice@x.com>, bob@x.com'],
      ['non-string To', {to: 123, cc: 'a@x.com, b@x.com'}, 'me@x.com', 'a@x.com, b@x.com'],
      ['empty Cc', {to: 'me@x.com, a@x.com', cc: ''}, 'me@x.com', 'a@x.com'],
    ];
    for (const [label, message, own, expected] of cases) {
      assert.equal(buildReplyAllCc(message, own), expected, label);
    }
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
