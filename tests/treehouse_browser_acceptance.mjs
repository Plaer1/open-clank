#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const snapshot = {
  accountId: 'acct-bob', workspace: 'school', actor: { id: 'acct-bob', displayName: 'Bob' },
  permissions: { admin: true, author: true, learner: true, analytics: true, grade: true },
  courseCapabilities: { 'course:shared': { learn: true, edit: false, owner: false, author: false } },
  state: {
    revision: 1, profiles: { 'acct-bob': { id: 'acct-bob', roles: ['admin', 'instructor', 'learner'], active: true } },
    courses: { 'course:shared': { id: 'course:shared', title: 'Shared', description: 'Read only', status: 'published', moduleIds: [] } },
    modules: {}, activities: {}, assignments: {}, skills: {}, badges: {}, quests: {}, courseGrants: {}, enrollments: {}, submissions: {}, evidence: {}, events: [],
  },
  projection: { eventCount: 0, learners: { 'acct-bob': { points: 0, badges: [], quests: [], courses: {}, skills: {}, pointEvidence: [] } }, leaderboard: [], courses: {} },
};

const page = `<!doctype html><html><body><main id="treehouse"></main><script type="module">
(async () => { try {
const { configureCopalStorage } = await import('/static/js/copal/storage.js');
const { createTreeHouseFeature } = await import('/static/js/copal/treehouse.js');
configureCopalStorage('browser-treehouse');
window.styledConfirm = async message => { window.__resetPreview = message; return false; };
const h = (tag, attrs = {}, ...children) => { const node = document.createElement(tag); for (const [key, value] of Object.entries(attrs)) { if (key === 'text') node.textContent = value; else if (key === 'onclick') node.addEventListener('click', value); else if (key === 'class') node.className = value; else if (key === 'aria-label') node.setAttribute(key, value); else if (key !== 'selected' && key !== 'disabled') node[key] = value; } for (const child of children) node.append(child instanceof Node ? child : document.createTextNode(String(child))); return node; };
window.__snapshot = ${JSON.stringify(snapshot)};
window.__feature = createTreeHouseFeature({ h, api: async () => window.__snapshot, setStatus() {}, renderMarkdown: text => document.createTextNode(text), openDocument() {} });
window.__feature.loadState(); window.__feature.render(document.querySelector('#treehouse')).catch(error => { window.__renderError = error.stack || String(error); });
} catch (error) { window.__renderError = error.stack || String(error); } })();
</script></body></html>`;

test('real browser mounts TreeHouse scope preview and per-mode context', async () => {
  await withCopalBrowser({ page }, async ({ cdp, evaluate, until }) => {
    await until("window.__renderError || document.querySelector('button.copal-btn.danger')?.textContent === 'Reset my progress'");
    assert.equal(await evaluate('window.__renderError'), undefined);
    const controls = await evaluate("[...document.querySelectorAll('button')].map(button => button.textContent)");
    assert.equal(controls.some(label => /^(Edit|Delete|Share)$/.test(label)), false);
    assert.equal(await evaluate("!!document.querySelector('[role=region][aria-label=\"TreeHouse workspace\"]')"), true);
    await evaluate("document.querySelector('.copal-treehouse-nav button').focus()");
    assert.equal(await evaluate("document.activeElement === document.querySelector('.copal-treehouse-nav button')"), true);
    await cdp('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:1, mobile:false });
    assert.equal(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), true);
    await cdp('Emulation.setEmulatedMedia', { features:[{ name:'prefers-reduced-motion', value:'reduce' }] });
    assert.equal(await evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"), true);
    await evaluate("document.querySelector('button.copal-btn.danger').click()");
    const preview = await evaluate('window.__resetPreview');
    assert.match(preview, /1 visible course/);
    assert.match(preview, /Bob in school/);
    assert.match(preview, /curricula and other learners stay intact/i);
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Open').click()");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Admin').click()");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Analytics').click()");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Learner').click()");
    assert.equal(await evaluate("localStorage.getItem('odysseus-treehouse-section:acct-bob:learner:scope:browser-treehouse:school')"), 'courses');
    assert.equal(await evaluate("localStorage.getItem('odysseus-treehouse-course:acct-bob:learner:scope:browser-treehouse:school')"), 'course:shared');
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Admin').click()");
    assert.equal(await evaluate("localStorage.getItem('odysseus-treehouse-section:acct-bob:admin:scope:browser-treehouse:school')"), 'analytics');
  });
});
