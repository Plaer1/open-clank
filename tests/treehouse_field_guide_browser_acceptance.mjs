#!/usr/bin/env node

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

// This is a disposable mounted exercise for the published Field Guide.  It
// deliberately checks the learner-facing rows and links for every course,
// instead of treating a manifest or a single representative lesson as
// coverage for the catalogue.
const SURFACES = [
  ['fg-orientation', 'treehouse', '/copal/treehouse'],
  ['fg-assistant', 'assistant', '/'],
  ['fg-editor', 'editor', '/copal/editor'],
  ['fg-files', 'files', '/files'],
  ['fg-wiki', 'wiki', '/copal/wiki'],
  ['fg-bases', 'bases', '/copal/bases'],
  ['fg-timeline', 'timeline', '/copal/timeline'],
  ['fg-connections', 'graph', '/copal/graph'],
  ['fg-tasks', 'tasks', '/copal/todo'],
  ['fg-settings', 'settings', '/settings'],
  ['fg-continuity', 'continuity', '/'],
  ['fg-teaching', 'teaching', '/copal/treehouse'],
  ['fg-models', 'models', '/settings'],
  ['fg-automation', 'automation', '/'],
  ['fg-research-media', 'research', '/'],
  ['fg-communications', 'communications', '/'],
  ['fg-operations', 'operations', '/settings'],
];

const fieldGuideState = JSON.parse(execFileSync(process.env.PYTHON_BIN || 'python3', ['-c', [
  'import json',
  'from src.openclank.copal_treehouse import new_treehouse_state',
  'from src.openclank.treehouse_field_guide import instantiate_field_guide',
  "print(json.dumps(instantiate_field_guide(new_treehouse_state('acct-field-guide'), 'acct-field-guide')))",
].join('\n')], { cwd: process.cwd(), encoding: 'utf8' }));

const fieldGuideLessons = Object.fromEntries(Object.values(fieldGuideState.activities).map(activity => [activity.fieldGuideKey, activity]));
const snapshot = {
  accountId: 'acct-field-guide',
  workspace: 'field-guide-workspace',
  actor: fieldGuideState.profiles['acct-field-guide'],
  permissions: { admin: false, author: false, learner: true, analytics: false, grade: false },
  courseCapabilities: Object.fromEntries(Object.values(fieldGuideState.courses).map(course => [course.id, { learn: true, edit: false, owner: false, author: false }])),
  state: fieldGuideState,
  projection: { eventCount: 0, learners: { 'acct-field-guide': { points: 0, badges: [], quests: [], courses: {}, skills: {}, pointEvidence: [] } }, leaderboard: [], courses: {} },
};

const page = `<!doctype html><html><body><main id="treehouse"></main><script type="module">
(async () => { try {
  const { configureCopalStorage } = await import('/static/js/copal/storage.js');
  const { createTreeHouseFeature } = await import('/static/js/copal/treehouse.js');
  configureCopalStorage('field-guide-mounted');
  const h = (tag, attrs = {}, ...children) => { const node = document.createElement(tag); for (const [key, value] of Object.entries(attrs)) { if (key === 'text') node.textContent = value; else if (key === 'onclick') node.addEventListener('click', value); else if (key === 'class') node.className = value; else if (key.startsWith('aria-') || key.startsWith('data-')) node.setAttribute(key, String(value)); else if (key !== 'selected' && key !== 'disabled') node[key] = value; } for (const child of children) node.append(child instanceof Node ? child : document.createTextNode(String(child))); return node; };
  window.__snapshot = ${JSON.stringify(snapshot)};
  window.__feature = createTreeHouseFeature({ h, api: async () => window.__snapshot, setStatus() {}, renderMarkdown: text => document.createTextNode(text), openDocument() {} });
  window.__feature.loadState(); await window.__feature.render(document.querySelector('#treehouse'));
} catch (error) { window.__renderError = error.stack || String(error); } })();
</script></body></html>`;

test('mounted Field Guide exposes all 17 disposable course journeys', async () => {
  await withCopalBrowser({ page }, async ({ evaluate, until }) => {
    await until("window.__renderError || document.querySelectorAll('.copal-treehouse-course').length === 17", '17 Field Guide courses');
    assert.equal(await evaluate('window.__renderError'), undefined);
    for (const [courseKey, surface, href] of SURFACES) {
      const course = Object.values(fieldGuideState.courses).find(item => item.fieldGuideKey === courseKey);
      assert(course, `Python Field Guide manifest has ${courseKey} course`);
      await evaluate(`document.querySelector('[data-treehouse-id="${course.id}"] button')?.click()`);
      await until(`document.querySelector('[data-field-guide-surface="${surface}"]')`, `${courseKey} practice`);
      const result = await evaluate(`(() => { const row=document.querySelector('[data-field-guide-surface="${surface}"]'); const link=row?.querySelector('a'); return { fixture:row?.querySelector('.copal-treehouse-practice')?.textContent || '', href:link?.getAttribute('href') || '', lesson:row?.dataset?.fieldGuideLesson || '' }; })()`);
      const expected = fieldGuideLessons[`${courseKey}:lesson-1`];
      assert(expected, `Python Field Guide manifest has ${courseKey} lesson`);
      assert(result.fixture.includes(expected.practice.title), `${courseKey} practice title is rendered`);
      assert(result.fixture.includes(expected.practice.expectedEvidence), `${courseKey} verifier evidence is rendered`);
      assert.equal(result.href, expected.surface.href);
      assert.equal(result.href, href);
      assert.equal(result.lesson, expected.fieldGuideKey);
      assert.equal(result.lesson, `${courseKey}:lesson-1`);
    }
  });
});
