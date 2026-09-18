#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body>
<textarea id="editor">alpha beta</textarea><textarea id="editor-two">gamma delta</textarea><a href="/target">target</a>
<script type="module">
  window.__setupMenu = async () => {
    const { initCustomContextMenu } = await import('/static/js/custom-context-menu.js?context-browser');
    let clipboard = '';
    Object.defineProperty(navigator, 'clipboard', { configurable:true, value:{
      writeText: async value => { clipboard = String(value); window.__clipboard = clipboard; },
      readText: async () => clipboard,
    }});
    initCustomContextMenu();
  };
  window.__menuState = () => ({
    labels:[...document.querySelectorAll('#openclank-context-menu button')].map(button => button.textContent),
    value:document.querySelector('#editor').value,
    selection:[document.querySelector('#editor').selectionStart, document.querySelector('#editor').selectionEnd],
    clipboard:window.__clipboard || '',
    prevented:window.__prevented === true,
  });
</script></body>`;

await withCopalBrowser({ page }, async ({ evaluate, until }) => {
  await until('window.__setupMenu');
  await evaluate('window.__setupMenu()');
  await evaluate(`(() => {
    const input = document.querySelector('#editor'); input.focus(); input.setSelectionRange(6, 10);
    const event = new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:10, clientY:10 });
    window.__prevented = !input.dispatchEvent(event); return window.__prevented;
  })()`);
  await until('document.querySelectorAll("#openclank-context-menu button").length > 0');
  const opened = await evaluate('window.__menuState()');
  assert.equal(opened.prevented, true);
  assert(opened.labels.includes('Copy'));
  assert(!opened.labels.some(label => /browser menu/i.test(label)), 'native browser handoff must not be offered');
  assert.deepEqual(opened.selection, [6, 10], 'selection belongs to the original textarea');
  // A second right-click replaces the first captured request while the menu
  // is open. Copy must come from the second textarea, proving target recapture.
  await evaluate(`(() => {
    const input = document.querySelector('#editor-two'); input.focus(); input.setSelectionRange(6, 11);
    input.dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:40, clientY:40 }));
  })()`);
  await until('document.querySelector("#openclank-context-menu")');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=copy]").click()');
  await until('window.__clipboard === "delta"');
  await evaluate(`(() => {
    const input = document.querySelector('#editor'); input.focus(); input.setSelectionRange(6, 10);
    input.dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:10, clientY:10 }));
  })()`);
  await until('document.querySelector("#openclank-context-menu")');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=cut]").click()');
  await until('document.querySelector("#editor").value === "alpha "');
  assert.equal((await evaluate('window.__menuState()')).clipboard, 'beta');
  // Moving a touch pointer cancels the pending long-press context menu.
  await evaluate(`(() => {
    const input = document.querySelector('#editor-two');
    input.dispatchEvent(new PointerEvent('pointerdown', { bubbles:true, pointerType:'touch', clientX:10, clientY:10 }));
    input.dispatchEvent(new PointerEvent('pointermove', { bubbles:true, pointerType:'touch', clientX:30, clientY:10 }));
  })()`);
  await new Promise(resolve => setTimeout(resolve, 650));
  assert.equal(await evaluate('!!document.querySelector("#openclank-context-menu")'), false, 'touch movement cancels long press');
  await evaluate(`(() => {
    const input = document.querySelector('#editor-two');
    input.dispatchEvent(new PointerEvent('pointerdown', { bubbles:true, pointerType:'touch', clientX:10, clientY:10 }));
    document.dispatchEvent(new Event('scroll', { bubbles:true }));
  })()`);
  await new Promise(resolve => setTimeout(resolve, 650));
  assert.equal(await evaluate('!!document.querySelector("#openclank-context-menu")'), false, 'scroll cancels long press');
  console.log('App-owned context menu browser path: ordinary browser activation, captured textarea selection, clipboard copy/cut, and no native Browser menu handoff passed.');
});
