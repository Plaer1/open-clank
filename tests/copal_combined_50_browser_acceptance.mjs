#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const PRODUCTION_JOURNEYS = [
  ['theme-pink-default', 'tests/theme_browser_acceptance.mjs', 'test'],
  ['shared-provider-label', 'tests/model_labels.mjs', 'script'],
  ['modelled-children', 'tests/provider_add_models_browser_acceptance.mjs', 'script'],
  ['template-source', 'tests/copal_host_template_provider_browser_acceptance.mjs', 'script'],
  ['multicursor-multiline', 'tests/copal_multicursor_browser_acceptance.mjs', 'script'],
  ['rich-comments-source', 'tests/copal_multicursor_browser_acceptance.mjs', 'script'],
  ['files-history-treehouse', 'tests/copal_mounted_editor_memes_acceptance.mjs', 'script', [
    ['tests/history_settings_production_browser_acceptance.mjs', 'script'],
    ['tests/treehouse_field_guide_browser_acceptance.mjs', 'test'],
  ]],
  ['locale-account-reload-persistence', 'tests/i18n_browser_acceptance.mjs', 'script'],
  ['reminders-retained', 'tests/reminder_settings_production_browser_acceptance.mjs', 'script'],
];

function runProductionJourney(label, fixture, mode, supplementalSpecs = []) {
  const run = (childFixture, childMode) => {
    const args = childMode === 'test' ? ['--test', childFixture] : [childFixture];
    const result = spawnSync(process.execPath, args, {
      cwd: process.cwd(),
      encoding: 'utf8',
      env: process.env,
      maxBuffer: 16 * 1024 * 1024,
    });
    const output = [result.stdout, result.stderr].filter(Boolean).join('\n').trim();
    assert.equal(result.status, 0, `${label} · ${childFixture} exited ${result.status ?? 'unknown'}\n${output.slice(-4000)}`);
    return { fixture: childFixture, command: [process.execPath, ...args].join(' '), exitCode: result.status, output: output.slice(-1200) };
  };
  const primary = run(fixture, mode);
  if (supplementalSpecs.length) return { fixtures: [primary, ...supplementalSpecs.map(([childFixture, childMode]) => run(childFixture, childMode))] };
  return primary;
}

const sidebarPage = `<!doctype html><html><head><link rel="stylesheet" href="/static/style.css"></head><body>
<button id="hamburger-btn" aria-label="Open navigation">☰</button>
<aside id="sidebar"></aside><nav id="icon-rail" class="rail-hidden"></nav><div id="sidebar-backdrop"></div><div id="chat-container"></div>
<script type="module">
  import Storage from '/static/js/storage.js';
  import { initSidebarLayout } from '/static/js/sidebar-layout.js';
  Storage.set(Storage.KEYS.SIDEBAR_SIDE, 'right');
  initSidebarLayout(Storage, {
    documentModule: { swapSide() {} },
    _closeCompareIfActive() {},
    _deactivateIncognito() {},
    presetsModule: {},
    sessionModule: {},
    el: id => document.getElementById(id),
    _defaultChat: {},
    _syncResearchIndicator() {},
  });
  window.__sidebarReady = true;
</script></body></html>`;

test('production qualification gate composes isolated child journeys and one mounted handoff', async () => {
  const receipts = {};
  for (const [label, fixture, mode, supplementalSpecs] of PRODUCTION_JOURNEYS) {
    receipts[label] = runProductionJourney(label, fixture, mode, supplementalSpecs);
  }

  await withCopalBrowser({ page: sidebarPage }, async ({ evaluate, until }) => {
    await until('window.__sidebarReady', 'production sidebar module');
    await until('document.body.classList.contains("hamburger-right")', 'right handed hamburger');
    assert.equal(await evaluate('document.querySelector("#hamburger-btn")?.getBoundingClientRect().right <= innerWidth + 1'), true);
    receipts['hamburger-right'] = {
      fixture: 'static/js/sidebar-layout.js',
      command: 'withCopalBrowser(sidebarPage)',
      exitCode: 0,
      output: 'initSidebarLayout mounted the production hamburger and applied hamburger-right',
    };
  });

  assert.deepEqual(Object.keys(receipts).sort(), [...PRODUCTION_JOURNEYS.map(([label]) => label), 'hamburger-right'].sort());
  process.stdout.write(JSON.stringify({ labels: receipts }) + '\n');
});
