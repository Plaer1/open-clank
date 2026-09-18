#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><script type="module">
  window.__setup = async () => {
    const { createSpellingService } = await import('/static/js/copal/spelling.js?spelling-browser');
    localStorage.clear();
    window.__spelling = createSpellingService('/static/js/copal/spelling-worker.js?spelling-browser', { scope:{ accountId:'account-a', workspace:'editor-a' }, locale:'fr-FR' });
    await window.__spelling.ready;
  };
</script></body>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__setup');
  await evaluate('window.__setup()');
  await until('window.__spelling');
  assert.equal(await evaluate('window.__spelling.locale().supported'), false, 'unsupported locales are reported honestly');
  assert.equal(await evaluate('window.__spelling.locale().dictionaryLocale'), 'en-US', 'unsupported locales use the documented English fallback');
  assert.equal(await evaluate('window.__spelling.check("editor")'), true);
  assert.equal(await evaluate('window.__spelling.check("running")'), true, 'Hunspell morphology must accept inflected forms');
  assert.equal(await evaluate('window.__spelling.check("edtor")'), false);
  assert.equal(await evaluate('window.__spelling.check("zzzzzz")'), false);
  await evaluate('window.__spelling.add(["OpenClankPersonal"])');
  assert.equal(await evaluate('window.__spelling.check("OpenClankPersonal")'), true);
  await evaluate('window.__spelling.add(["SurvivingPersonal"])');
  await evaluate('window.__spelling.remove(["OpenClankPersonal"])');
  assert.equal(await evaluate('window.__spelling.check("OpenClankPersonal")'), false);
  assert.equal(await evaluate('window.__spelling.check("SurvivingPersonal")'), true, 'removing one personal word preserves others');
  await evaluate('window.__spelling.destroy()');
  await evaluate(`(async () => { const { createSpellingService } = await import('/static/js/copal/spelling.js?spelling-isolated'); window.__spelling = createSpellingService('/static/js/copal/spelling-worker.js?spelling-isolated', { scope:{ accountId:'account-b', workspace:'editor-a' }, locale:'en-US' }); await window.__spelling.ready; })()`);
  assert.equal(await evaluate('window.__spelling.check("SurvivingPersonal")'), false, 'personal words are isolated per account/workspace');
  await evaluate('window.__spelling.destroy()');
  await evaluate(`(async () => { const { createSpellingService } = await import('/static/js/copal/spelling.js?spelling-restart'); window.__spelling = createSpellingService('/static/js/copal/spelling-worker.js?spelling-restart', { scope:{ accountId:'account-a', workspace:'editor-a' }, locale:'fr-FR' }); await window.__spelling.ready; })()`);
  assert.equal(await evaluate('window.__spelling.check("SurvivingPersonal")'), true, 'personal words survive worker restart');
  assert((await evaluate('window.__spelling.suggest("edtor")')).includes('editor'));
  console.log('Frozen spelling worker browser path: async readiness, deterministic check/suggest, and personal-word add/remove passed.');
});
