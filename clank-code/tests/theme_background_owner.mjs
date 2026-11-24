import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

test('reapplying a live background retains its canvas and animation owner', async () => {
  const page = `<!doctype html><body><main id="chat-container"></main>
    <script type="module">
      import { applyBgPattern } from '/static/js/theme.js';
      window.applyPattern = applyBgPattern;
    </script></body>`;
  const overrides = { '/static/js/ui.js': 'export default { showToast() {}, closeAllDropdowns() {} };' };
  await withCopalBrowser({ page, overrides }, async ({ evaluate, until }) => {
    await until('typeof window.applyPattern === "function"', 'theme module');
    for (const pattern of ['clanker-kene-weave', 'clanker-emoji-drift']) {
      await evaluate(`window.applyPattern(${JSON.stringify(pattern)})`);
      const selector = JSON.stringify('#' + pattern + '-canvas');
      await until(`document.querySelector(${selector})?.isConnected && typeof window.__openClankBackgroundOwner === 'function'`, pattern);
      const result = await evaluate(`(() => {
        const canvas = document.querySelector(${selector});
        const owner = window.__openClankBackgroundOwner;
        const original = window.requestAnimationFrame;
        let scheduled = 0;
        window.requestAnimationFrame = function (cb) { scheduled++; return original.call(window, cb); };
        try {
          for (let i = 0; i < 10; i++) window.applyPattern(${JSON.stringify(pattern)});
          return {
            sameCanvas: document.querySelector(${selector}) === canvas,
            sameOwner: window.__openClankBackgroundOwner === owner,
            count: document.querySelectorAll(${selector}).length,
            scheduled
          };
        } finally { window.requestAnimationFrame = original; }
      })()`);
      assert.deepEqual(result, { sameCanvas: true, sameOwner: true, count: 1, scheduled: 0 }, pattern);
      await evaluate("window.applyPattern('none')");
      assert.equal(await evaluate(`document.querySelector(${selector}) === null && !window.__openClankBackgroundOwner`), true);
    }
  });
});
