#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><style>body{font:12px sans-serif}.copal-task-list{height:640px;overflow:auto}</style><main id="tasks"></main>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('document.readyState === "complete"');
  await evaluate(`(async () => {
    const { createPlanningFeature } = await import('/static/js/copal/planning.js?renderer-perf=1');
    const h = (tag, attrs = {}, ...children) => {
      const node = document.createElement(tag);
      for (const [key, value] of Object.entries(attrs || {})) {
        if (key === 'text') node.textContent = String(value);
        else if (key === 'class') node.className = String(value);
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
        else if (value != null && value !== false) node.setAttribute(key, String(value));
      }
      for (const child of children.flat()) if (child) node.append(child.nodeType ? child : document.createTextNode(String(child)));
      return node;
    };
    const items = Array.from({ length: 5000 }, (_, index) => ({
      id: 'task-' + index, source: index % 2 ? 'markdown' : 'vault',
      text: 'Expanded renderer task ' + index, label: 'note-' + index + '.md', checked: index % 3 === 0,
      task: { text: 'Expanded renderer task ' + index, done: index % 3 === 0 },
      doc: { id: 'doc-' + index, name: 'note-' + index + '.md' },
    }));
    const planning = createPlanningFeature({
      h, api: async () => ({}), getPlanning: () => ({ tracks: [], floatingTodos: [] }),
      refresh: async () => {}, setStatus: () => {}, projectionChanged: () => {}, openDocument: () => {},
      openMarkdownTask: () => {}, createMarkdownTask: () => {}, patchMarkdownTask: async () => {},
    });
    const body = document.querySelector('#tasks');
    const render = () => { planning.renderTodo(body, items, { total: 5000, indexedTotal: 5000, matchedTotal: 5000, totalExact: true }); void body.offsetHeight; };
    render();
    const samples = [];
    for (let index = 0; index < 30; index += 1) {
      const start = performance.now(); render(); samples.push(performance.now() - start);
    }
    samples.sort((a, b) => a - b);
    const list = body.querySelector('.copal-tasks-list');
    const eventBody = document.createElement('main'); document.body.append(eventBody);
    const events = Array.from({ length: 4000 }, (_, index) => ({ id:'event-' + index, title:'Expanded event ' + index, description:'event description', status:index % 4 ? 'pending' : 'done', priority:index % 3 ? 'medium' : 'high', startDate:'2026-09-' + String((index % 28) + 1).padStart(2, '0'), dueDate:null, trackId:null, tags:[], stages:[] }));
    const eventPlanning = createPlanningFeature({ h, api: async () => ({}), getPlanning: () => ({ tracks: [{ id:'event-track', name:'Events', tasks:events }], floatingTodos: [] }), refresh: async () => {}, setStatus: () => {}, projectionChanged: () => {}, openDocument: () => {}, openMarkdownTask: () => {}, createMarkdownTask: () => {}, patchMarkdownTask: async () => {} });
    const eventRender = () => { eventPlanning.renderTodo(eventBody, [], { total: 0, totalExact: true }); void eventBody.offsetHeight; };
    eventRender(); const eventSamples = [];
    for (let index = 0; index < 30; index += 1) { const start = performance.now(); eventRender(); eventSamples.push(performance.now() - start); }
    eventSamples.sort((a, b) => a - b);
    const eventList = eventBody.querySelector('.copal-tasks-list');
    window.__rendererPerf = { samples, medianMs: samples[14], p95Ms: samples[Math.ceil(samples.length * .95) - 1], mountedRows: body.querySelectorAll('.copal-task-row').length, descendants: body.querySelectorAll('*').length, scrollHeight: list?.scrollHeight || 0, total: 5000, events: { samples:eventSamples, medianMs:eventSamples[14], p95Ms:eventSamples[Math.ceil(eventSamples.length * .95) - 1], mountedRows:eventBody.querySelectorAll('.copal-task-row').length, scrollHeight:eventList?.scrollHeight || 0, total:4000 } };
  })()`);
  await until('window.__rendererPerf');
  const result = await evaluate('window.__rendererPerf');
  assert.ok(result.mountedRows > 0 && result.mountedRows < 200);
  assert.ok(result.scrollHeight >= result.total * 40);
  assert.equal(result.events.samples.length, 30);
  assert.ok(result.events.mountedRows > 0 && result.events.mountedRows < 200);
  assert.ok(result.events.scrollHeight >= result.events.total * 40);
  assert.ok(result.p95Ms <= 100 && result.events.p95Ms <= 100);
  assert.equal(result.samples.length, 30);
  console.log(JSON.stringify({ fixture: '5,000 expanded task rows (virtual window)', ...result }));
});
