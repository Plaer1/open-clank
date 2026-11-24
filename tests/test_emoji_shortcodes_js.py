"""Emoji shortcodes honor token boundaries and leave ratios and unknown codes intact."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_emoji_shortcode_rendering():
    script = r"""
    import assert from 'node:assert/strict';
    import { hasEmojiShortcode, replaceEmojiShortcodes } from './static/js/emojiShortcodes.js';
    assert.equal(hasEmojiShortcode(':blush:'), true);
    assert.equal(hasEmojiShortcode('no shortcodes here'), false);
    assert.equal(hasEmojiShortcode('a single : colon'), false);
    const cases = [
      ["visit today? :blush:", "visit today? 😊"],
      ["hobbies? **:microphone:**", "hobbies? **🎤**"],
      [":fire:", "🔥"],
      [":tada:", "🎉"],
      [":thinking:", "🤔"],
      [":+1:", "👍"],
      [":thumbsup:", "👍"],
      ["nice :fire: work :100:", "nice 🔥 work 💯"],
      [":definitely_not_an_emoji:", ":definitely_not_an_emoji:"],
      [":emoji:", ":emoji:"],
      ["meet at 10:30:45 today", "meet at 10:30:45 today"],
      ["ratio 16:9 vs 4:3", "ratio 16:9 vs 4:3"],
      ["plain text", "plain text"],
      ["1:100:2", "1:100:2"],
      ["scale 3:100:7 ok", "scale 3:100:7 ok"],
      ["host:fire:port", "host:fire:port"],
      ["status:fire:", "status:fire:"],
      [":fire:done", ":fire:done"],
      ["we hit :100: today", "we hit 💯 today"],
      ["see :fire:!", "see 🔥!"],
      [":fire::tada:", "🔥🎉"],
    ];
    for (const [input, expected] of cases) {
      assert.deepEqual(replaceEmojiShortcodes(input), expected, JSON.stringify(input));
    }
    """
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
