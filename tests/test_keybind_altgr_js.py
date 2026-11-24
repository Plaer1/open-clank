"""AltGr typing must not trigger shortcuts. Mac Option shortcuts remain usable."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_altgr_and_combo_matching():
    script = r"""
    import assert from 'node:assert/strict';
    import { isAltGrEvent } from './static/js/platform.js';
    import { _matchesCombo } from './static/js/keyboard-shortcuts.js';

    function event(ctrlKey, altKey, altGraph, key = 'n') {
      const ev = { key, ctrlKey, altKey, shiftKey: false, metaKey: false };
      if (altGraph !== undefined) ev.getModifierState = mod => mod === 'AltGraph' && altGraph;
      return ev;
    }
    const predicateCases = [
      [true, true, true, false, true], [true, true, false, false, false],
      [false, false, true, false, false], [true, false, true, false, false],
      [false, true, true, false, false], [true, true, true, true, false],
      [true, true, undefined, false, false],
    ];
    for (const [ctrl, alt, altGraph, mac, expected] of predicateCases) {
      assert.equal(isAltGrEvent(event(ctrl, alt, altGraph), mac), expected, JSON.stringify({ctrl, alt, altGraph, mac}));
    }
    const combos = [
      [event(true, true, true), 'ctrl+alt+n', false, false],
      [event(true, true, false), 'ctrl+alt+n', false, true],
      [event(true, true, true), 'ctrl+alt+n', true, true],
      [event(true, false, false, 'k'), 'ctrl+k', false, true],
    ];
    for (const [ev, combo, mac, expected] of combos) {
      assert.equal(_matchesCombo(ev, combo, mac), expected, JSON.stringify({ev, combo, mac}));
    }
    for (const combo of [123, {}, null, 'k']) {
      assert.equal(_matchesCombo(event(false, false, false, 'k'), combo), combo === 'k', JSON.stringify(combo));
    }
    // Each platform case needs a fresh module evaluation because IS_MAC is computed at import time.
    const platforms = [
      ['MacIntel', '', true],
      ['MacIntel', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15', true],
      ['', 'Mozilla/5.0 (Macintosh; ...)', true],
      ['Win32', 'Mozilla/5.0 (Windows NT 10.0)', false],
    ];
    for (const [index, [platform, userAgent, expected]] of platforms.entries()) {
      Object.defineProperty(globalThis, 'navigator', { value: {platform, userAgent}, configurable: true });
      const { IS_MAC } = await import('./static/js/platform.js?case=' + index);
      assert.equal(IS_MAC, expected, JSON.stringify({platform, userAgent}));
    }
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
