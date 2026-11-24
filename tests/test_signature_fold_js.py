"""Signature folding and quoted-reply attribution from Gmail and Outlook."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_signature_and_quote_headers():
    script = r"""
    import assert from 'node:assert/strict';
    const { _extractQuoteMeta, _foldSignature } = await import('./static/js/emailLibrary/signatureFold.js');
    globalThis.document = {
      querySelector: selector => selector === "link[data-oc-icons]" ? {} : null,
      createElement: () => ({
        set textContent(value) { this.text = value; },
        get innerHTML() { return this.text || ''; }
      })
    };
    assert.equal(_extractQuoteMeta(null), '');
    assert.equal(_extractQuoteMeta({bad: true}), '');
    const outlook = 'From: Alice <alice@example.com> Sent: Monday, May 4, 2026 To: Bob Subject: hi';
    assert.equal(_extractQuoteMeta(outlook), 'Alice · Monday, May 4, 2026');
    const weekday = _extractQuoteMeta('On Mon, Apr 18, 2026 at 9:31 AM, Jane Doe &lt;jane@example.com&gt; wrote:');
    assert.ok(weekday.startsWith('Jane Doe jane@example.com'), weekday);
    assert.ok(weekday.includes('Mon, Apr 18, 2026'), weekday);
    assert.equal(_extractQuoteMeta('On Wed, Jan 1, 2025, Jane wrote:'), 'Jane · Wed, Jan 1, 2025');
    assert.match(_extractQuoteMeta('On Apr 18, 2026 at 9:31 AM, Jane Doe wrote:'), /^Jane Doe · Apr 18, 2026/);
    assert.match(_extractQuoteMeta('On Mon, 18 Apr 2026 at 09:31, Jane Doe &lt;jane@example.com&gt; wrote:'), /^Jane Doe jane@example.com/);

    for (const br of ['<br />', '<br/>', '<br>']) {
      const html = ['Hello.', '-- ', 'John Smith', 'Acme', 'X'.repeat(250)].join(br);
      assert.ok(_foldSignature(html, null).includes('email-sig-fold'), br);
    }
    assert.equal(_foldSignature('Hello.<br />-- <br />JS', null).includes('email-sig-fold'), false);
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
