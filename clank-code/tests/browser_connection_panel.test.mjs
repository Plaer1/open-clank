import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const panelSource = await readFile(new URL('../static/js/browser/connectionPanel.js', import.meta.url), 'utf8');
const { mountBrowserConnectionPanel } = await import(`data:text/javascript;base64,${Buffer.from(panelSource).toString('base64')}`);

class Element {
  constructor(tagName) { this.tagName = tagName; this.children = []; this.listeners = new Map(); this.open = false; }
  append(...items) { for (const item of items) { item.parent = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  addEventListener(name, callback) { this.listeners.set(name, [...(this.listeners.get(name) || []), callback]); }
  dispatch(name, event = {}) { for (const callback of this.listeners.get(name) || []) callback({ target: this, ...event }); }
  setAttribute() {}
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); }
  findText(text) { if (this.textContent === text) return this; for (const child of this.children) { const hit = child.findText?.(text); if (hit) return hit; } return null; }
  textContent = '';
}

test('opening the mounted Connection menu loads current tabs and Refresh reloads them', async () => {
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  const documentListeners = new Map();
  globalThis.document = {
    createElement: tag => new Element(tag),
    addEventListener: (name, callback) => documentListeners.set(name, callback),
    removeEventListener: name => documentListeners.delete(name),
  };
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(String(url));
    return { ok: true, json: async () => ({ connected: true, tabs: [
      { index: 2, current: true, title: 'Synthetic tab', url: 'https://example.test/' },
    ] }) };
  };
  try {
    const header = new Element('header');
    const panel = mountBrowserConnectionPanel(header, { currentSession: () => 'chat-synthetic', status: () => {} });
    const menu = header.children[0];
    menu.open = true;
    menu.dispatch('toggle');
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(requests.length, 1);
    assert.match(requests[0], /session_id=chat-synthetic/);
    assert.ok(menu.findText('Connected · 1 tab(s)'));
    assert.ok(menu.findText('✓ Synthetic tab · https://example.test/'));
    menu.findText('Refresh tabs').dispatch('click');
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.equal(requests.length, 2);
    await panel.close();
  } finally {
    globalThis.document = previousDocument;
    globalThis.fetch = previousFetch;
  }
});
