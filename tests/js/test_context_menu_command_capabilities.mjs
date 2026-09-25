import assert from 'node:assert/strict';
import { createCodeMirrorContextAdapter } from '../../static/js/custom-context-menu.js';

// Minimal editor stub: selection state only. The adapter's commands() predicate
// is what this test pins — selection-only actions disabled without a selection.
function makeEditor(selectedText = '') {
  const body = 'line one\nline two';
  const state = {
    doc: {
      toString: () => body,
      length: body.length,
      sliceString: (from, to) => body.slice(from, to),
    },
    selection: { main: { anchor: 0, head: selectedText.length } },
  };
  return {
    view: {
      state,
      dom: { isConnected: true, dataset: {} },
      posAtCoords: () => 0,
    },
    getSelection: () => ({
      mainIndex: 0,
      ranges: [{ anchor: 0, head: selectedText.length }],
    }),
    getSelectedText: () => selectedText,
  };
}

function requestFor(selectedText) {
  const editor = makeEditor(selectedText);
  const adapter = createCodeMirrorContextAdapter(editor, {
    bufferIdentity: 'buf-1',
    revision: 'r1',
    scope: 'a:w:1',
  });
  const capture = adapter.capture();
  return { adapter, request: { adapterContext: capture, editable: true, target: null } };
}

// Empty selection: selection-only commands disabled, line/cursor commands on.
{
  const { adapter, request } = requestFor('');
  const commands = adapter.commands(request);
  const byId = Object.fromEntries(commands.map((c) => [c.id, c]));
  assert.equal(byId['select-next-match'].disabled, true, 'select-next-match disabled without selection');
  assert.equal(byId['select-all-matches'].disabled, true, 'select-all-matches disabled without selection');
  assert.equal(byId['format-bold'].disabled, true, 'bold disabled without selection');
  assert.equal(byId['format-italic'].disabled, true, 'italic disabled without selection');
  assert.equal(byId['format-code'].disabled, true, 'inline code disabled without selection');
  assert.equal(byId['duplicate-line'].disabled, undefined, 'duplicate-line stays usable');
  assert.equal(byId['indent'].disabled, undefined, 'indent stays usable');
  assert.equal(byId['insert-template'].disabled, undefined, 'insert template stays usable');
  assert.equal(byId['new-from-template'].disabled, undefined, 'new from template stays usable');
}

// Non-empty selection: selection-only commands enabled.
{
  const { adapter, request } = requestFor('selected text');
  const commands = adapter.commands(request);
  const byId = Object.fromEntries(commands.map((c) => [c.id, c]));
  assert.equal(byId['select-next-match'].disabled, false, 'select-next-match enabled with selection');
  assert.equal(byId['format-bold'].disabled, false, 'bold enabled with selection');
  assert.equal(byId['duplicate-line'].disabled, undefined);
}

// Template commands dispatch through the identity onCommand hook.
{
  const calls = [];
  const editor = makeEditor('');
  const adapter = createCodeMirrorContextAdapter(editor, {
    bufferIdentity: 'buf-1',
    revision: 'r1',
    scope: 'a:w:1',
    onCommand: async (command) => { calls.push(command); return true; },
  });
  const capture = adapter.capture();
  const request = { adapterContext: capture, editable: true, target: null };
  const handledInsert = await adapter.execute('insert-template', request);
  assert.equal(handledInsert, true);
  assert.deepEqual(calls, ['insert-template']);
  const handledNew = await adapter.execute('new-from-template', request);
  assert.equal(handledNew, true);
  assert.deepEqual(calls, ['insert-template', 'new-from-template']);

  // Without a handler the command falls through (returns false).
  const bare = createCodeMirrorContextAdapter(makeEditor(''), { bufferIdentity: 'b', revision: 'r', scope: 's' });
  const bareReq = { adapterContext: bare.capture(), editable: true, target: null };
  assert.equal(await bare.execute('insert-template', bareReq), false);
}

console.log('context-menu command capability tests passed');
