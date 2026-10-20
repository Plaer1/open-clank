import assert from 'node:assert/strict';
import { createCodeMirrorContextAdapter } from '../../static/js/custom-context-menu.js';

// Minimal mounted CodeMirror contract. Selection transforms require text;
// select-next can select the word at a cursor when that capability exists.
function makeEditor(selectedText = '') {
  const body = 'line one\nline two';
  const state = {
    doc: {
      toString: () => body,
      length: body.length,
      sliceString: (from, to) => body.slice(from, to),
    },
    selection: {
      main: { anchor: 0, head: selectedText.length },
      mainIndex: 0,
      ranges: [{ anchor: 0, head: selectedText.length }],
    },
  };
  return {
    view: {
      state,
      dom: { isConnected: true, dataset: {}, querySelectorAll: () => [] },
      posAtCoords: () => 0,
    },
    getSelection: () => state.selection,
    getSelectedText: () => selectedText,
    selectNextMatch: () => true,
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
  assert.equal(byId['select-next-match'].disabled, false, 'select-next-match may select the cursor word');
  assert.equal(byId['select-all-matches'].disabled, true, 'select-all-matches disabled without selection');
  assert.equal(byId['format-bold'].disabled, true, 'bold disabled without selection');
  assert.equal(byId['format-italic'].disabled, true, 'italic disabled without selection');
  assert.equal(byId['format-code'].disabled, true, 'inline code disabled without selection');
  assert.equal(byId['duplicate-line'].disabled, undefined, 'duplicate-line stays usable');
  assert.equal(byId['indent'].disabled, undefined, 'indent stays usable');
  assert.equal(byId['insert-template'].disabled, true, 'insert template needs the owning editor callback');
  assert.equal(byId['new-from-template'].disabled, true, 'new from template needs the owning editor callback');
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

// Text alone does not invent a missing editor capability.
{
  const editor = makeEditor('line');
  delete editor.selectNextMatch;
  const adapter = createCodeMirrorContextAdapter(editor);
  const request = { adapterContext: adapter.capture(), editable: true, target: null };
  assert.equal(adapter.commands(request).find((c) => c.id === 'select-next-match').disabled, true);
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
  for (const id of ['insert-template', 'new-from-template']) {
    assert.equal(adapter.commands(request).find((c) => c.id === id).disabled, false);
  }
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

// A callback cannot make a read-only editor writable or revive a stale capture.
{
  const editor = makeEditor('line');
  let invoked = 0;
  editor.view.contentDOM = { getAttribute: () => 'false' };
  const adapter = createCodeMirrorContextAdapter(editor, {
    onCommand: async () => { invoked++; return true; },
  });
  const request = { adapterContext: adapter.capture(), editable: false, target: null };
  for (const id of ['format-bold', 'insert-template', 'new-from-template']) {
    assert.equal(adapter.commands(request).find((c) => c.id === id).disabled, true);
  }
  editor.view.state.selection.ranges = [{ anchor: 1, head: 3 }];
  assert.equal(adapter.isCurrent(request), false);
  await assert.rejects(adapter.execute('insert-template', request));
  assert.equal(invoked, 0);
}

console.log('context-menu command capability tests passed');
