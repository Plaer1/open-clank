import test from 'node:test';
import assert from 'node:assert/strict';
// The shared renderer builds DOM text nodes; provide a minimal document shim
// for this Node test. Production runs in a browser with a real document.
if (typeof globalThis.document === 'undefined') {
  globalThis.document = {
    visibilityState: 'visible',
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
  extractReferenceSection,
  mediaKind,
} from '../../static/js/copal/markdownResources.js';

test('Markdown rendering and app destinations', async (t) => {
  const previousFrame = globalThis.requestAnimationFrame;
  globalThis.requestAnimationFrame = callback => queueMicrotask(callback);
  t.after(() => {
    if (previousFrame) globalThis.requestAnimationFrame = previousFrame;
    else delete globalThis.requestAnimationFrame;
  });
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
      return { destinationExists: true, element: { isConnected: true, getClientRects: () => [{ width: 200, height: 100 }] } };
    });
    assert.ok(listAppDestinations().includes('testscreen'));

    const ok = await openAppDestination('clank://testscreen/panel-a');
    assert.equal(ok.ok, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].screen, 'testscreen');
    assert.equal(calls[0].panel, 'panel-a');

    // Unknown destinations never execute anything and produce a useful error.
    const unknown = await openAppDestination('clank://no-such-screen');
    assert.equal(unknown.ok, false);
    assert.match(unknown.error, /Unknown app destination/);
    assert.equal(calls.length, 1, 'unknown destination does not invoke handlers');

    // Handler failures are reported, not thrown.
    registerAppDestination('brokenscreen', () => { throw new Error('boom'); });
    const failed = await openAppDestination('clank://brokenscreen');
    assert.equal(failed.ok, false);
    assert.match(failed.error, /boom/);

    dispose();
    const afterDispose = await openAppDestination('clank://testscreen');
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
});

test('Markdown resources resolve paths, media types and heading sections', () => {
  const origin = { id:'note', name:'Notes/旅行.md' };
  const documents = [origin, { id:'local', name:'Notes/photo one.png', kind:'asset' }, { id:'other', name:'Other/photo one.png', kind:'asset' }, { id:'root', name:'root.png', kind:'asset' }, { id:'alias', name:'Manual/Guide.md', properties:{ aliases:['Help'] } }];
  const resolve = source => resolveReference(findReferenceToken(source), { documents, origin });
  assert.equal(resolve('before ![](./photo%20one.png) after').target.id, 'local');
  assert.equal(resolve('![[photo one.png|240x120]] trailing').target.id, 'local');
  assert.equal(resolve('![](../root.png)').target.id, 'root');
  assert.equal(resolve('![](/root.png)').target.id, 'root');
  assert.equal(resolve('![](../../root.png)').status, 'missing');
  assert.equal(resolve('![](./missing.png)').status, 'missing');
  assert.equal(resolve('[[Help#Getting started|Read this]]').target.id, 'alias');
  assert.equal(resolve('[[#Heading]]').target.id, 'note');
  assert.equal(resolveReference(findReferenceToken('![[photo one.png]]'), { documents }).status, 'ambiguous');
  assert.equal(resolve('![](file:///tmp/private.png)').status, 'unsupported');
  assert.equal(resolve('![](https://example.invalid/image.png)').status, 'external');
  const reference = findReferenceToken('before ![alt](<./photo (one).png> "Photo title") after');
  assert.equal(reference.index, 7);
  assert.equal(reference.target, './photo (one).png');
  assert.equal(reference.title, 'Photo title');
  assert.equal('before ![alt](<./photo (one).png> "Photo title") after'.slice(reference.index + reference.length), ' after');
  assert.equal(findReferenceToken('\\!\\[\\[not a link\\]\\]'), null);
  assert.equal(findReferenceToken('![broken](unclosed'), null);
  assert.equal(mediaKind({ name:'recording.wav' }), 'audio');
  assert.equal(mediaKind({ name:'film.webm' }), 'video');
  assert.equal(mediaKind({ name:'manual.pdf' }), 'pdf');
  const source = '# Top\nintro\n## Kept\nwanted\n```md\n## Fake\n```\n### Nested\nmore\n## Omitted\nother\n';
  const section = extractReferenceSection(source, 'Kept');
  assert.equal(section.line, 3);
  assert(section.text.includes('wanted'));
  assert(section.text.includes('### Nested'));
  assert(!section.text.includes('Omitted'));
  assert.equal(extractReferenceSection('# Same\none\n# Same\ntwo', 'Same').status, 'ambiguous');
  assert.equal(extractReferenceSection(source, 'Fake').status, 'missing');
  assert.equal(extractReferenceSection('first paragraph\n\nblock first\nblock last ^keep\n\nafter', '^keep').text, 'block first\nblock last ^keep');
  console.log('Copal shared Markdown reference resolution passed');
});
