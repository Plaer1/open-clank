import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><main id="app"></main></body>`;

// The 1 MiB workload plus real Markdown rendering and the revision
// catch-up probe need more than the 15s default evaluate window.
await withCopalBrowser({ page, cdpTimeoutMs: 60000 }, async ({ evaluate }) => {
  const result = await evaluate(`(async () => {
    const { createSourceEditor } = await import('/static/js/copal/codemirror.js?rich-performance=' + Date.now());
    // Real shared Markdown renderer: widget cost must include rendering.
    const { createMarkdownRenderer } = await import('/static/js/copal/markdownRenderer.js?rich-performance=' + Date.now());
    const h = (tag, attrs = {}, ...children) => {
      const node = document.createElement(tag);
      for (const [key, value] of Object.entries(attrs || {})) {
        if (key === 'text') node.textContent = value;
        else if (key === 'class') node.className = value;
        else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
        else if (value != null) node.setAttribute(key, String(value));
      }
      for (const child of children.flat()) if (child != null) node.append(child.nodeType ? child : document.createTextNode(String(child)));
      return node;
    };
    const renderer = createMarkdownRenderer({
      h,
      documents: () => [],
      findByName: () => null,
      assetUrl: () => null,
      openTarget: () => {},
    });
    const host = document.createElement('div');
    host.style.cssText = 'width:800px;height:180px';
    document.body.append(host);
    const targetBytes = 1024 * 1024;
    const repeated = '// # Performance comment\\r\\nconst value = 1;\\r\\n';
    let source = '';
    while (new TextEncoder().encode(source).byteLength < targetBytes) source += repeated;
    source = source.slice(0, targetBytes);
    while (new TextEncoder().encode(source).byteLength < targetBytes) source += ' ';
    const editor = createSourceEditor({
      parent:host,
      doc:source,
      language:'JavaScript',
      richComments:true,
      renderPreview:markdown => { try { return renderer.renderMarkdown(markdown); } catch (_) { return null; } },
    });
    await editor.languageReady;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const bytes = new TextEncoder().encode(editor.getValue()).byteLength;
    const samples = [];
    for (let index = 0; index < 9; index += 1) {
      editor.setSelection({ ranges:[{ anchor:0, head:0 }, { anchor:17, head:17 }], mainIndex:index % 2 });
      const started = performance.now();
      editor.insertText(index % 2 ? 'x' : 'y');
      samples.push(performance.now() - started);
      editor.undo();
      await new Promise(resolve => requestAnimationFrame(resolve));
    }
    const sorted = [...samples].sort((a, b) => a - b);
    const percentile = fraction => sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
    // Revision catch-up: edits arriving during a pending parse must still
    // reparse the newest document (not discard and stall).
    editor.setSelection({ ranges:[{ anchor:0, head:0 }] });
    editor.insertText('// brand-new note\\n');
    editor.insertText('z');
    await new Promise(resolve => setTimeout(resolve, 0));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const revisionCaughtUp = [...host.querySelectorAll('.cm-rich-comment-widget')].some(node => node.textContent.includes('brand-new note'));
    const metrics = { bytes, samples, p50:percentile(0.5), p95:percentile(0.95), max:sorted.at(-1), widgets:host.querySelectorAll('.cm-rich-comment-widget').length, revisionCaughtUp };
    editor.destroy(); host.remove();
    return metrics;
  })()`);
  assert.equal(result.bytes, 1024 * 1024, 'workload must preserve exactly 1 MiB of source bytes');
  assert.ok(result.widgets > 0, 'the rich-comment workload must mount visible widgets');
  assert.ok(result.p95 <= 50, `rich-comment edits exceeded the 50ms p95 budget: ${JSON.stringify(result)}`);
  assert.equal(result.revisionCaughtUp, true, 'rapid edits during a pending parse must still reparse the newest revision');
  console.log(`Copal rich-comment 1 MiB performance: ${JSON.stringify(result)}`);
});
