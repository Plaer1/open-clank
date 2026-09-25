import assert from 'node:assert/strict';
// The shared renderer builds DOM text nodes; provide a minimal document shim
// for this Node test. Production runs in a browser with a real document.
if (typeof globalThis.document === 'undefined') {
  globalThis.document = {
    createTextNode: (value) => ({ nodeType: 3, textContent: String(value ?? '') }),
  };
}
import {
  createMarkdownRenderer,
  registerAppDestination,
  openAppDestination,
  listAppDestinations,
} from '../../static/js/copal/markdownRenderer.js';
import {
  createReferenceRenderer,
  resolveReference,
  findReferenceToken,
} from '../../static/js/copal/markdownResources.js';

// ── clank:// app destination recognition in the resolver ──
{
  const ref = { target:'clank://settings/appearance', embed:false, syntax:'markdown', label:'Settings' };
  const resolved = resolveReference(ref, { documents:[] });
  assert.equal(resolved.status, 'app');
  assert.equal(resolved.destination, 'settings/appearance');

  const empty = resolveReference({ target:'clank://', embed:false }, { documents:[] });
  assert.equal(empty.status, 'missing', 'empty clank:// target is missing, not app');

  const external = resolveReference({ target:'https://example.com', embed:false }, { documents:[] });
  assert.equal(external.status, 'external', 'https still external');

  const unsupported = resolveReference({ target:'javascript:alert(1)', embed:false }, { documents:[] });
  assert.equal(unsupported.status, 'unsupported', 'other schemes stay unsupported');
}

// ── app destination registry ──
{
  const calls = [];
  const dispose = registerAppDestination('testscreen', ({ screen, panel, destination }) => {
    calls.push({ screen, panel, destination });
  });
  assert.ok(listAppDestinations().includes('testscreen'));

  const ok = openAppDestination('clank://testscreen/panel-a');
  assert.equal(ok.ok, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].screen, 'testscreen');
  assert.equal(calls[0].panel, 'panel-a');

  // Unknown destinations never execute anything and produce a useful error.
  const unknown = openAppDestination('clank://no-such-screen');
  assert.equal(unknown.ok, false);
  assert.match(unknown.error, /Unknown app destination/);
  assert.equal(calls.length, 1, 'unknown destination does not invoke handlers');

  // Handler failures are reported, not thrown.
  registerAppDestination('brokenscreen', () => { throw new Error('boom'); });
  const failed = openAppDestination('clank://brokenscreen');
  assert.equal(failed.ok, false);
  assert.match(failed.error, /boom/);

  dispose();
  const afterDispose = openAppDestination('clank://testscreen');
  assert.equal(afterDispose.ok, false, 'disposed handler is not invoked');
}

// ── full renderer: plugin blocks stay inert, app links dispatch ──
{
  const elements = [];
  const h = (tag, attrs = {}, ...children) => {
    const el = {
      tag, attrs: attrs || {}, children: children.flat().filter(Boolean),
      classList: { add() {} },
      append(...nodes) { this.children.push(...nodes.flat().filter(Boolean)); },
      replaceChildren(...nodes) { this.children = nodes.flat().filter(Boolean); },
      addEventListener() {},
      set textContent(value) { this.attrs.text = value; },
      get textContent() {
        const collect = (node) => {
          if (node == null) return '';
          if (typeof node === 'string') return node;
          if (node.attrs?.text) return node.attrs.text;
          return (node.children || []).map(collect).join('');
        };
        return collect(this);
      },
    };
    elements.push(el);
    return el;
  };
  const docs = [
    { id:'d1', name:'Alpha.md', kind:'markdown', text:'# Alpha' },
    { id:'d2', name:'Beta.md', kind:'markdown', text:'# Beta' },
  ];
  const opened = [];
  const appOpened = [];
  const renderer = createMarkdownRenderer({
    h,
    documents:() => docs,
    findByName:(name) => docs.find((doc) => doc.name === name || doc.name === `${name}.md`) || null,
    openTarget:(target, fragment, event) => opened.push({ id:target?.id, fragment, hasEvent:!!event }),
    openAppDestination:(destination, event) => { appOpened.push({ destination, hasEvent:!!event }); return { ok:true }; },
    onConvertPluginBlock:null,
  });

  // Full block rendering: heading, task, code fence.
  const md = renderer.renderMarkdown('# Title\n\n- [ ] todo item\n\n```js\nconst x = 1;\n```\n');
  const flat = [];
  const walk = (node) => { if (!node) return; flat.push(node); for (const child of node.children || []) walk(child); };
  walk(md);
  const heading = flat.find((el) => el.tag === 'h1');
  assert.ok(heading, 'heading rendered');
  const task = flat.find((el) => el.attrs?.class?.includes?.('copal-markdown-task'));
  assert.ok(task, 'task rendered');
  const code = flat.find((el) => el.tag === 'code' && el.attrs?.['data-language'] === 'js');
  assert.ok(code, 'fenced code rendered');
  const pluginBanner = flat.find((el) => el.attrs?.class?.includes?.('copal-plugin-block-banner'));
  assert.equal(pluginBanner, undefined, 'ordinary code has no plugin banner');

  // Plugin query block is inert (banner present, no execution).
  const pluginMd = renderer.renderMarkdown('```tasks\nnot done\n```\n');
  const pluginFlat = [];
  const walk2 = (node) => { if (!node) return; pluginFlat.push(node); for (const child of node.children || []) walk2(child); };
  walk2(pluginMd);
  const inertBanner = pluginFlat.find((el) => el.attrs?.class?.includes?.('copal-plugin-block-banner'));
  assert.ok(inertBanner, 'plugin block shows inert banner');

  // Internal link click carries the event through openTarget.
  const linkMd = renderer.renderMarkdown('See [[Alpha]]\n');
  const linkFlat = [];
  const walk3 = (node) => { if (!node) return; linkFlat.push(node); for (const child of node.children || []) walk3(child); };
  walk3(linkMd);
  const chip = linkFlat.find((el) => el.tag === 'button' && el.attrs?.class === 'copal-chip');
  assert.ok(chip, 'internal link renders as chip');
  chip.attrs.onclick?.({ ctrlKey:true });
  assert.equal(opened.length, 1);
  assert.equal(opened[0].id, 'd1');
  assert.equal(opened[0].hasEvent, true, 'openTarget receives the click event');

  // clank:// markdown link dispatches to the app destination registry.
  const appMd = renderer.renderMarkdown('Open [Settings](clank://settings/appearance)\n');
  const appFlat = [];
  const walk4 = (node) => { if (!node) return; appFlat.push(node); for (const child of node.children || []) walk4(child); };
  walk4(appMd);
  const appChip = appFlat.find((el) => el.attrs?.['data-app-destination']);
  assert.ok(appChip, 'clank:// link renders as app chip');
  assert.equal(appChip.attrs['data-app-destination'], 'settings/appearance');
  appChip.attrs.onclick?.({});
  assert.equal(appOpened.length, 1);
  assert.match(appOpened[0].destination, /settings\/appearance/);

  // External URLs stay external anchors, never app destinations.
  const extMd = renderer.renderMarkdown('[Example](https://example.com)\n');
  const extFlat = [];
  const walk5 = (node) => { if (!node) return; extFlat.push(node); for (const child of node.children || []) walk5(child); };
  walk5(extMd);
  const anchor = extFlat.find((el) => el.tag === 'a' && el.attrs?.href === 'https://example.com');
  assert.ok(anchor, 'external URL stays an anchor');
}

// ── createReferenceRenderer passes the click event to openTarget ──
{
  const h = (tag, attrs = {}, ...children) => ({
    tag, attrs: attrs || {}, children: children.flat().filter(Boolean),
    classList: { add() {} }, append(...n) { this.children.push(...n); }, addEventListener() {},
  });
  const target = { id:'x', name:'X.md', kind:'markdown', text:'# X' };
  const calls = [];
  const render = createReferenceRenderer({
    h,
    documents:() => [target],
    assetUrl:() => null,
    openTarget:(t, fragment, event) => calls.push({ t, fragment, hasEvent:!!event }),
    renderDocument:() => h('div'),
  });
  const node = render({ target:'X.md', embed:false, syntax:'markdown', label:'X' }, { origin:null, seen:new Set() });
  node.attrs.onclick({ metaKey:true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].hasEvent, true);
  assert.equal(calls[0].t.id, 'x');
}

console.log('copal markdown renderer / app destination tests passed');
