import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><main id="app"></main></body>`;

await withCopalBrowser({ page }, async ({ evaluate }) => {
  const result = await evaluate(`(async () => {
    const { createSourceEditor } = await import('/static/js/copal/codemirror.js?rich-performance=' + Date.now());
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
      renderPreview:markdown => { const node = document.createElement('strong'); node.textContent = markdown; return node; },
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
    const metrics = { bytes, samples, p50:percentile(0.5), p95:percentile(0.95), max:sorted.at(-1), widgets:host.querySelectorAll('.cm-rich-comment-widget').length };
    editor.destroy(); host.remove();
    return metrics;
  })()`);
  assert.equal(result.bytes, 1024 * 1024, 'workload must preserve exactly 1 MiB of source bytes');
  assert.ok(result.widgets > 0, 'the rich-comment workload must mount visible widgets');
  assert.ok(result.p95 <= 50, `rich-comment edits exceeded the 50ms p95 budget: ${JSON.stringify(result)}`);
  console.log(`Copal rich-comment 1 MiB performance: ${JSON.stringify(result)}`);
});
