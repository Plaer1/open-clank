#!/usr/bin/env node

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

// Mounted exercise for the published S30 Field Guide: five free-exploration
// Classes, thirty unique lessons, shared app-link destinations and disposable
// practice.  This checks learner-facing rows and links for every lesson.  It
// is rendering-and-destination evidence, not proof that each advertised
// Editor/Files/Imps exercise was executed against a live engine — that
// boundary is recorded honestly in the S30 receipt.
const guide = JSON.parse(execFileSync(process.env.PYTHON_BIN || 'python3', ['-c', [
  'import json',
  'from src.openclank.treehouse_field_guide import field_guide_manifest',
  'print(json.dumps(field_guide_manifest()))',
].join('\n')], { cwd: process.cwd(), encoding: 'utf8' }));

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

test('mounted Field Guide exposes five classes and thirty unique lessons', async () => {
  assert.equal(guide.courses.length, 5);
  assert.equal(guide.lessons.length, 30);
  assert.equal(new Set(guide.lessonKeys).size, 30);
  await withCopalBrowser({ page }, async ({ evaluate, until }) => {
    await until("window.__renderError || document.querySelectorAll('.copal-treehouse-course').length === 5", '5 Field Guide classes');
    assert.equal(await evaluate('window.__renderError'), undefined);
    for (const courseSpec of guide.courses) {
      const course = Object.values(fieldGuideState.courses).find(item => item.fieldGuideKey === courseSpec.key);
      assert(course, `Python Field Guide manifest has ${courseSpec.key} class`);
      await evaluate(`document.querySelector('[data-treehouse-id="${course.id}"] button')?.click()`);
      for (const lessonSpec of courseSpec.lessons) {
        const activity = fieldGuideLessons[lessonSpec.key];
        assert(activity, `Python Field Guide manifest has ${lessonSpec.key} lesson`);
        await until(`document.querySelector('[data-field-guide-lesson="${lessonSpec.key}"]')`, `${lessonSpec.key} row`);
        const result = await evaluate(`(() => { const row=document.querySelector('[data-field-guide-lesson="${lessonSpec.key}"]'); const link=row?.querySelector('a[data-app-destination]'); return { fixture:row?.querySelector('.copal-treehouse-practice')?.textContent || '', destination:link?.getAttribute('data-app-destination') || '', href:link?.getAttribute('href') || '', surface:row?.dataset?.fieldGuideSurface || '' }; })()`);
        assert.equal(result.destination, lessonSpec.surface.appLink, `${lessonSpec.key} app link`);
        assert.equal(result.surface, lessonSpec.surface.key, `${lessonSpec.key} surface key`);
        assert.equal(result.href, lessonSpec.surface.href, `${lessonSpec.key} canonical href`);
        assert(!result.href.includes('/copal/'), `${lessonSpec.key} avoids legacy /copal/*`);
        if (lessonSpec.practice) {
          assert(result.fixture.includes(lessonSpec.practice.title), `${lessonSpec.key} practice title is rendered`);
          assert(result.fixture.includes(lessonSpec.practice.expectedEvidence), `${lessonSpec.key} verifier evidence is rendered`);
        }
      }
    }
  });
});

test('built-in classes carry no prerequisite locks and hide ultra-rares', async () => {
  for (const course of guide.courses) {
    assert.deepEqual(course.prerequisites, [], `${course.key} has no prerequisite lock`);
    assert.equal(course.freeExploration, true);
  }
  const ultraIds = guide.lessons.flatMap((lesson) => (lesson.achievementHints || []).filter((hint) => hint.rarity === 'ultra').map((hint) => hint.id));
  assert.deepEqual(ultraIds, [], 'no ultra-rare name leaks through a lesson hint');
  const secretHints = guide.lessons.flatMap((lesson) => (lesson.achievementHints || []).filter((hint) => hint.secret));
  for (const hint of secretHints) assert.equal(hint.rarity, 'mystery');
});
