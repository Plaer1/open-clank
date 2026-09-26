#!/usr/bin/env node

import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><html><head><meta name="theme-color" content="#000"><link rel="stylesheet" href="/static/style.css"></head><body><main id="workspace">Theme probe</main><button id="primary-probe" class="send-btn">Primary</button><script type="module">
  import * as theme from '/static/js/theme.js';
  window.__theme = theme;
  window.__themeReady = true;
</script></body></html>`;

test('mounted theme presets apply readable palettes and persist identity safely', async () => {
  await withCopalBrowser({ page }, async ({ evaluate, until }) => {
    await until('window.__themeReady', 'theme module', 30000);
    const light = await evaluate(`(() => { const result=window.__theme.applyTheme('clanker-light', null, { persist:false, storedOptions:{ bgPattern:'none' } }); return { result:!!result, class:document.body.className, bg:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(), fg:getComputedStyle(document.documentElement).getPropertyValue('--fg').trim(), meta:document.querySelector('meta[name="theme-color"]').content }; })()`);
    assert(light.result);
    assert(light.class.includes('theme-clanker-light'));
    assert.equal(light.bg, '#F3EEDB');
    assert.equal(light.fg, '#17202A');
    assert.equal(light.meta, '#F3EEDB');

    const dark = await evaluate(`(() => { const result=window.__theme.applyTheme('clanker-dark', null, { persist:false, storedOptions:{ bgPattern:'none' } }); return { result:!!result, class:document.body.className, bg:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(), fg:getComputedStyle(document.documentElement).getPropertyValue('--fg').trim(), scheme:getComputedStyle(document.documentElement).colorScheme }; })()`);
    assert(dark.result);
    assert(dark.class.includes('theme-clanker-dark'));
    assert.equal(dark.bg, '#191A1E');
    assert.equal(dark.fg, '#FFF4D6');
    assert.equal(dark.scheme, 'dark');

    const normalized = await evaluate(`(() => {
      const legacy = window.__theme.normalizeThemeSnapshot({
        name: 'clanker-dark',
        colors: { bg:'#191A1E', fg:'#FFF4D6', panel:'#25272C', border:'#555A62', red:'#5A9EF5' },
        bgPattern: 'none',
      });
      const custom = window.__theme.normalizeThemeSnapshot({
        name: 'probe-custom',
        colors: { bg:'#FAFAFA', fg:'#222222', panel:'#FFFFFF', border:'#CCCCCC' },
      });
      const forest = window.__theme.normalizeThemeSnapshot({
        name: 'forest',
        colors: { bg:'#1B2A1B', fg:'#A8D5A2', panel:'#142414', border:'#3D6B3D', red:'#7CB871' },
      });
      const explicit = window.__theme.normalizeThemeSnapshot({
        version: 2,
        identity: { name:'clanker-dark', overridePresent:true },
        colors: { bg:'#191A1E', fg:'#FFF4D6', panel:'#25272C', border:'#555A62', red:'#5A9EF5' },
      }, { name:'clanker-dark' });
      window.__theme.applyColors(custom.colors);
      const computedAccentValue = getComputedStyle(document.documentElement).getPropertyValue('--accent-primary').trim();
      window.__theme.applyTheme('clanker-light', null, { persist:false, storedOptions:{ bgPattern:'none' } });
      const buttonEl = document.getElementById('primary-probe');
      // Finish transitions so computed color reflects the settled theme.
      buttonEl.getAnimations().forEach(a => { try { a.finish(); } catch (_) {} });
      const button = getComputedStyle(buttonEl);
      return {
        legacyAccent: legacy.colors.red,
        legacyPattern: legacy.background.pattern,
        customAccent: custom.colors.red,
        customSend: custom.colors.advanced.sendBtnBg || null,
        customHover: custom.colors.advanced.accentHover,
        forestHover: forest.colors.advanced.accentHover,
        explicitAccent: explicit.colors.red,
        computedAccent: computedAccentValue,
        buttonBackground: button.backgroundColor,
        buttonForeground: button.color,
      };
    })()`);
    assert.equal(normalized.legacyAccent, '#F6BE48');
    assert.equal(normalized.legacyPattern, 'none');
    assert.equal(normalized.customAccent, '#F6BE48');
    assert.equal(normalized.customSend, null);
    assert.equal(normalized.customHover, '#F6BE48');
    assert.equal(normalized.forestHover, '#7CB871');
    assert.equal(normalized.explicitAccent, '#5A9EF5');
    assert.equal(normalized.computedAccent, '#F6BE48');
    const rgb = value => value.match(/\d+(?:\.\d+)?/g).map(Number).slice(0, 3);
    const luminance = value => rgb(value).map(channel => channel / 255).map(channel => channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4).reduce((sum, channel, index) => sum + channel * [0.2126, 0.7152, 0.0722][index], 0);
    const ratio = (Math.max(luminance(normalized.buttonBackground), luminance(normalized.buttonForeground)) + 0.05) / (Math.min(luminance(normalized.buttonBackground), luminance(normalized.buttonForeground)) + 0.05);
    assert(ratio >= 4.5, `filled primary contrast ${ratio.toFixed(2)}:1 is below 4.5:1`);
  });
});

test('danger and error affordances use semantic error tokens', () => {
  const css = fs.readFileSync(new URL('../static/style.css', import.meta.url), 'utf8');
  for (const selector of [
    '.session-bulk-btn-danger', '.footer-delete-btn:hover', '.msg-delete-btn:hover',
    '.gallery-bulk-delete:hover', '.task-btn-danger', '.gallery-editor-draft-delete:hover',
    '.note-card-delete:hover', '.note-delete-btn:hover', '.note-form-delete-btn:hover',
  ]) {
    const start = css.indexOf(selector);
    assert(start >= 0, `missing audited selector ${selector}`);
    const rule = css.slice(start, css.indexOf('}', start) + 1);
    assert.match(rule, /var\(--color-(?:danger|error)/, `${selector} still uses an accent-only token`);
  }
});

test('account hydration ignores stale responses across A to B to A switches', async () => {
  const accountPage = `<!doctype html><html><head><meta name="theme-color" content="#000"></head><body><main id="workspace">Account probe</main><script>
    window.__account = 'A';
    window.__pendingThemes = [];
    window.__hydrations = [];
    document.addEventListener('openclank:theme-account-hydrated', event => window.__hydrations.push(event.detail));
    window.fetch = async (input, init = {}) => {
      const path = new URL(input, location.href).pathname;
      const method = String(init.method || input?.method || 'GET').toUpperCase();
      if (path === '/api/auth/status') return new Response(JSON.stringify({ username: window.__account, account_id: window.__account }), { headers:{'content-type':'application/json'} });
      if (path === '/api/prefs/custom-themes') return new Response(JSON.stringify({ value:{} }), { headers:{'content-type':'application/json'} });
      if (path === '/api/prefs/theme' && method === 'GET') return await new Promise(resolve => window.__pendingThemes.push({ owner:window.__account, resolve }));
      if (path === '/api/prefs/theme') return new Response('{}', { headers:{'content-type':'application/json'} });
      return new Response('{}', { headers:{'content-type':'application/json'} });
    };
  </script><script type="module">
    import * as theme from '/static/js/theme.js';
    window.__theme = theme;
    window.__themeReady = true;
  </script></body></html>`;
  await withCopalBrowser({
    page: accountPage,
    overrides: { '/static/js/ui.js': 'export default { showToast() {}, closeAllDropdowns() {} };' },
  }, async ({ evaluate, until }) => {
    await until('window.__themeReady', 'theme module', 30000);
    const settle = async (owner, name, red) => {
      await until(`window.__pendingThemes.some(item => item.owner === ${JSON.stringify(owner)})`, `pending ${owner}`);
      await evaluate(`(() => { const index = window.__pendingThemes.findIndex(candidate => candidate.owner === ${JSON.stringify(owner)}); const item = window.__pendingThemes.splice(index, 1)[0]; item.resolve(new Response(JSON.stringify({ value:{ name:${JSON.stringify(name)}, colors:{ bg:'#191A1E', fg:'#FFF4D6', panel:'#25272C', border:'#555A62', red:${JSON.stringify(red)} } } }), { headers:{'content-type':'application/json'} })); return true; })()`);
    };
    await settle('A', 'clanker-dark', '#AA1111');
    await until('window.__hydrations.length >= 1', 'initial hydration');
    const initialState = await evaluate(`({ red:getComputedStyle(document.documentElement).getPropertyValue('--red').trim(), stored:localStorage.getItem('odysseus-theme:scope:A'), pending:window.__pendingThemes.length, hydrations:window.__hydrations })`);
    assert.equal(initialState.red, '#AA1111', JSON.stringify(initialState));

    await evaluate(`window.__account='B'; const detail={ username:'B', accountId:'B' }; document.dispatchEvent(new CustomEvent('openclank:auth-user-ready', { detail })); document.dispatchEvent(new CustomEvent('openclank:auth-context-changed', { detail }))`);
    await until(`window.__theme.themeStorageKey('B') === 'odysseus-theme:scope:B'`, 'B owner helper');
    assert.equal(await evaluate(`window.__theme.themeStorageKey()`), 'odysseus-theme:scope:B', 'no-arg helper tracks active owner');
    const afterSwitch = await evaluate(`({ pending:window.__pendingThemes.map(item => item.owner), hydrations:window.__hydrations.map(item => item.accountId) })`);
    assert.equal(afterSwitch.pending.filter(owner => owner === 'B').length, 1, JSON.stringify(afterSwitch));
    await settle('B', 'clanker-light', '#11AA11');
    await until('window.__hydrations.length >= 2', 'B hydration');
    assert.equal(await evaluate(`getComputedStyle(document.documentElement).getPropertyValue('--red').trim()`), '#11AA11');

    await evaluate(`window.__account='A'; document.dispatchEvent(new CustomEvent('openclank:auth-context-changed', { detail:{ username:'A', accountId:'A' } }))`);
    await until(`window.__pendingThemes.filter(item => item.owner === 'A').length >= 1`, 'second A hydration');
    await evaluate(`window.__account='B'; document.dispatchEvent(new CustomEvent('openclank:auth-context-changed', { detail:{ username:'B', accountId:'B' } }))`);
    await until(`window.__pendingThemes.filter(item => item.owner === 'B').length >= 1`, 'stale B hydration');
    await settle('A', 'clanker-dark', '#AA1111');
    // Stale A response is ignored; B's owner-scoped local snapshot survives.
    assert.equal(await evaluate(`getComputedStyle(document.documentElement).getPropertyValue('--red').trim()`), '#11AA11');
    await settle('B', 'clanker-light', '#11AA11');
    await until('window.__hydrations.length >= 3', 'fresh B hydration');

    await evaluate(`window.__account='A'; document.dispatchEvent(new CustomEvent('openclank:auth-context-changed', { detail:{ username:'A', accountId:'A' } }))`);
    await until(`window.__pendingThemes.filter(item => item.owner === 'A').length >= 1`, 'final A hydration');
    await settle('A', 'clanker-dark', '#AA1111');
    await until('window.__hydrations.length >= 4', 'final A hydration event');
    assert.equal(await evaluate(`getComputedStyle(document.documentElement).getPropertyValue('--red').trim()`), '#AA1111');
    assert.deepEqual(await evaluate(`window.__hydrations.map(item => item.accountId)`), ['A', 'B', 'B', 'A']);
  });
});
