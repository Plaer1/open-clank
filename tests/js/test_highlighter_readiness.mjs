import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

function loadHighlighter() {
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });
  const window = { __odysseusShiki: { ready } };
  const context = vm.createContext({ window, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync('static/js/highlighter.js', 'utf8'), context);
  return { window, resolveReady };
}

function codeNode(lang = 'javascript') {
  return {
    dataset: { lang },
    className: `language-${lang}`,
    textContent: 'const answer = 42;',
    innerHTML: '',
    isConnected: true,
    classList: { add() {} },
  };
}

test('highlighter queues concrete code elements and coalesces streaming latest source', async () => {
  const { window, resolveReady } = loadHighlighter();
  const code = codeNode();
  const root = { querySelectorAll: () => [code] };
  window.odysseusHighlight.highlightAll(root);
  const painter = window.odysseusHighlight.createStreamingPainter(0);
  painter.paint(code, 'const old = 1;');
  painter.paint(code, 'const latest = 2;');
  resolveReady({ codeToHtml: (source) => `<pre><code><span>${source}</span></code></pre>` });
  await window.odysseusHighlight.ready;
  assert.match(code.innerHTML, /latest/);
  assert.equal(window.odysseusHighlight.detect('flowchart TD\n  A --> B'), 'mermaid');
});
