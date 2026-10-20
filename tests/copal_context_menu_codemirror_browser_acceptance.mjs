#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><dialog open id="dialog"><div id="editor"></div></dialog><div id="object" class="copal-task-row" role="row" data-copal-context-object="task"><input type="checkbox" aria-label="Complete task"><button class="copal-task-title" type="button">Open task</button></div><section id="track" class="copal-track" data-copal-context-object="track"><button class="copal-track-edit" type="button">Edit track</button></section><div id="event" class="copal-event" role="button" data-copal-context-object="event">Event</div><svg><g id="graph" class="copal-graph-node" data-copal-context-object="graph" role="button"><circle /></g></svg><article id="tree" class="copal-treehouse-course" data-copal-context-object="treehouse"><button class="copal-btn" type="button">Open</button></article><button id="file" class="copal-file-row" data-copal-context-object="file" data-file-open-editor="true" data-file-capabilities="open" type="button">File</button>
<a id="link" href="https://example.com/context">link</a><img id="image" src="data:image/png;base64,AA==" alt="fixture">
<script type="module">
  window.__setup = async () => {
    const [{ createMarkdownEditor }, menu] = await Promise.all([
      import('/static/js/copal/codemirror.js?context-cm'),
      import('/static/js/custom-context-menu.js?context-cm'),
    ]);
    let clipboard = '';
    Object.defineProperty(navigator, 'clipboard', { configurable:true, value:{
      writeText: async value => { await new Promise(resolve => setTimeout(resolve, 80)); clipboard = String(value); window.__clipboard = clipboard; },
      readText: async () => { await new Promise(resolve => setTimeout(resolve, 80)); return clipboard; },
      write: async items => { window.__imageClipboard = items.length; },
    }});
    window.ClipboardItem = class ClipboardItem { constructor(value) { this.value = value; } };
    window.__openClankCopalContextCommand = async (command, target) => {
      if (command !== 'copy-image-bytes' || target.id !== 'image') return false;
      window.__imageCommand = command; return true;
    };
    menu.initCustomContextMenu();
    const editor = createMarkdownEditor({ parent:document.querySelector('#editor'), doc:'alpha beta gamma', mode:'source', language:'markdown' });
    window.__scope = 'account-a:workspace-a:1';
    window.__revision = 'r1';
    window.__bufferIdentity = 'buffer-a';
    const dispose = menu.registerAdapter(editor.view.dom, menu.createCodeMirrorContextAdapter(editor, {
      bufferIdentity:() => window.__bufferIdentity,
      revision:() => window.__revision,
      scope:() => window.__scope,
    }));
    window.__editor = editor; window.__dispose = dispose;
  document.querySelector('#object').addEventListener('copal-context-command', event => { window.__objectCommand = event.detail.command; });
  for (const id of ['track','event','graph','tree','file']) document.querySelector('#'+id).addEventListener('click', () => { window.__opened = id; });
  document.querySelector('#object .copal-task-title').addEventListener('click', () => { window.__opened = 'task'; });
  document.querySelector('#file').addEventListener('copal-context-command', event => { if (event.detail?.command === 'open-in-editor') window.__opened = 'file'; });
  };
  window.__state = () => JSON.stringify({ text:window.__editor.getValue(), clipboard:window.__clipboard || '', labels:[...document.querySelectorAll('#openclank-context-menu button')].map(node => node.textContent), left:document.querySelector('#openclank-context-menu')?.style.left || '', focus:String(document.activeElement?.className || ''), object:window.__objectCommand || '' });
</script></body>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__setup'); await evaluate('window.__setup()');
  await until('!!window.__editor');
  await evaluate(`(() => { const e=window.__editor; e.view.dispatch({selection:{anchor:6,head:10}}); e.view.focus(); e.view.contentDOM.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:9999,clientY:9999})); })()`);
  await until('document.querySelector("#openclank-context-menu")');
  const opened = JSON.parse(await evaluate('window.__state()'));
  assert(opened.labels.includes('Copy')); assert(Number.parseFloat(opened.left) >= 8, 'menu placement is clamped to the viewport');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=copy]").click(); document.body.focus()');
  await until('window.__clipboard === "beta"');
  await evaluate(`(() => { const e=window.__editor; e.view.dispatch({selection:{anchor:6,head:10}}); e.view.contentDOM.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:20,clientY:20})); })()`);
  await until('document.querySelector("#openclank-context-menu")');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=cut]").click(); document.body.focus()');
  await until('window.__editor.getValue() === "alpha  gamma"');
  await evaluate(`(() => { const e=window.__editor; e.view.dispatch({selection:{anchor:6,head:6}}); e.view.contentDOM.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:20,clientY:20})); })()`);
  await until('document.querySelector("#openclank-context-menu")');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=paste]").click(); document.body.focus()');
  await until('window.__editor.getValue() === "alpha beta gamma"');
  // A same-length intervening edit must invalidate the captured CodeMirror
  // range; document length alone cannot detect this race.
  await evaluate(`(() => {
    const e=window.__editor;
    e.view.dispatch({changes:{from:0,to:e.view.state.doc.length,insert:'alpha beta gamma'},selection:{anchor:6,head:10}});
    e.view.contentDOM.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:20,clientY:20}));
  })()`);
  await until('document.querySelector("#openclank-context-menu")');
  await evaluate(`(() => {
    document.querySelector('#openclank-context-menu button[data-command=paste]').click();
    window.__editor.view.dispatch({changes:{from:6,to:10,insert:'BETA'}});
  })()`);
  await new Promise(resolve => setTimeout(resolve, 180));
  assert.equal(await evaluate('window.__editor.getValue()'), 'alpha BETA gamma', 'same-length edit invalidates async paste');
  // Scope changes (account/workspace generation) invalidate an otherwise
  // unchanged buffer before clipboard completion.
  await evaluate(`(() => {
    const e=window.__editor;
    e.view.dispatch({changes:{from:0,to:e.view.state.doc.length,insert:'alpha beta gamma'},selection:{anchor:6,head:6}});
    e.view.contentDOM.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:20,clientY:20}));
  })()`);
  await until('document.querySelector("#openclank-context-menu")');
  await evaluate(`(() => {
    document.querySelector('#openclank-context-menu button[data-command=paste]').click();
    window.__scope = 'account-b:workspace-b:2';
  })()`);
  await new Promise(resolve => setTimeout(resolve, 180));
  assert.equal(await evaluate('window.__editor.getValue()'), 'alpha beta gamma', 'scope change invalidates async paste');
  await evaluate(`(() => {
    window.openClankSpelling = { suggest: async word => word === 'edtor' ? ['editor'] : [], add:async()=>{}, remove:async()=>{} };
    const e=window.__editor; e.setValue('edtor'); e.view.dispatch({selection:{anchor:0,head:5}});
    e.view.contentDOM.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:20,clientY:20}));
  })()`);
  await until('document.querySelector("#openclank-context-menu button[data-command^=replace-spelling]")', 'spelling replacement suggestion');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command^=replace-spelling]").click()');
  await until('window.__editor.getValue() === "editor"', 'spelling replacement applies to captured range');
  await evaluate("document.querySelector('#link').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:20,clientY:20}))");
  await until('document.querySelector("#openclank-context-menu button[data-command=copy-link-address]")', 'link context actions');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=copy-link-address]").click()');
  await until('window.__clipboard === "https://example.com/context"', 'copy link address');
  await evaluate("document.querySelector('#image').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:20,clientY:20}))");
  await until('document.querySelector("#openclank-context-menu button[data-command=copy-image-bytes]")', 'image context actions');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=copy-image-bytes]").click()');
  await until('window.__imageCommand === "copy-image-bytes"', 'copy image bytes');
  await evaluate(`(() => { const target=document.querySelector('#object'); target.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:10,clientY:10})); })()`);
  await until('document.querySelector("#openclank-context-menu button[data-command=toggle-task]")');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=toggle-task]").click()');
  assert.equal(await evaluate('window.__objectCommand'), 'toggle-task');
  assert.equal(await evaluate('document.querySelector("#object input").checked'), true, 'task context command invokes the mounted task checkbox');
  const objectCommands = [['object','open-task','task'],['track','edit-track','track'],['event','edit-event','event'],['graph','open-graph-node','graph'],['tree','open-treehouse-item','tree'],['file','open-in-editor','file']];
  for (const [id, command, opened] of objectCommands) {
    await evaluate(`document.querySelector('#${id}').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:12,clientY:12}))`);
    await until(`document.querySelector('#openclank-context-menu button[data-command=${JSON.stringify(command)}]')`, `${id} ${command} context command`);
    await evaluate(`document.querySelector('#openclank-context-menu button[data-command=${JSON.stringify(command)}]').click()`);
    await until(`window.__opened === ${JSON.stringify(opened)}`);
  }
  await evaluate(`(() => { const e=window.__editor; e.view.focus(); document.body.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,detail:0,clientX:30,clientY:30})); })()`);
  await until('document.querySelector("#openclank-context-menu")');
  await evaluate('document.querySelector("#openclank-context-menu button").dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowDown",bubbles:true}))');
  assert.equal(await evaluate('document.activeElement?.tagName'), 'BUTTON', 'keyboard invocation keeps menu focus on a command');
  await evaluate('document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
  assert.equal(await evaluate('!!document.querySelector("#openclank-context-menu")'), false, 'Escape dismisses keyboard-invoked menu');
  await evaluate(`(() => { localStorage.setItem('odysseus-custom-context-menu','off'); window.dispatchEvent(new Event('odysseus-context-menu-changed')); })()`);
  assert.equal(await evaluate('!!document.querySelector("#openclank-context-menu")'), false, 'disabling the setting closes an open menu immediately');
  console.log('App-owned CodeMirror context path: native editor selection, async clipboard focus changes, viewport clamp, object command dispatch, and immediate disable passed.');
});
