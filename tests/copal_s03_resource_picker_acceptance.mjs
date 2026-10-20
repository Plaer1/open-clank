#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><script type="module">
window.__run = async () => {
  const {createResourcePicker} = await import('/static/js/copal/resourcePicker.js?s03-picker');
  const lifecycle = new Set(['openclank-account-changed','openclank-files-policy-changed','openclank-policy-changed','openclank-window-closed','openclank:auth-context-changed','openclank:file-policy-changed']);
  let adds = 0; let removes = 0;
  const add = window.addEventListener; const remove = window.removeEventListener;
  window.addEventListener = function(type, ...args) { if (lifecycle.has(type)) adds += 1; return add.call(this, type, ...args); };
  window.removeEventListener = function(type, ...args) { if (lifecycle.has(type)) removes += 1; return remove.call(this, type, ...args); };
  const rows = Array.from({length:5001}, (_, index) => ({ref:'file-'+index, id:'host-file-'+index, provider:'host', kind:'file', name:'file-'+index+'.md', capabilities:['read','open'], revision:{kind:'hostFingerprint',value:'r-'+index}}));
  const client = {roots:async()=>({policy_generation:3,entries:[{ref:'host-root',id:'host-root',provider:'host',kind:'folder',name:'Host',capabilities:['read','stat','children']}]}), children:async()=>({entries:rows,next_cursor:null,policy_generation:3}), search:async()=>({entries:[]})};
  const picker = createResourcePicker({client,purpose:'file'}); await picker.open(); await picker.enter(picker.state().rows[0]);
  const list = document.querySelector('[data-resource-picker-list]');
  const bound = list.querySelectorAll('[data-resource-ref]').length;
  const first = Boolean(list.querySelector('[data-row-index="0"]'));
  list.querySelector('[data-row-index="0"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true}));
  for (let index=0; index<2500; index += 1) list.querySelector('[data-row-index="' + index + '"]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));
  const middle = list.querySelector('[data-row-index="2500"]') !== null;
  list.querySelector('[data-row-index="2500"]').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}));
  await new Promise(resolve => requestAnimationFrame(resolve));
  const last = Boolean(list.querySelector('[data-row-index="5000"]'));
  picker.close(); await picker.open(); picker.close(); picker.destroy();
  window.addEventListener = add; window.removeEventListener = remove;
  return {bound,first,middle,last,adds,removes};
};
</script></body>`;

await withCopalBrowser({page}, async ({evaluate, until}) => {
  await until('document.readyState === "complete"');
  const result = await evaluate('window.__run()');
  assert(result.bound <= 240, `virtual picker mounted ${result.bound} rows`);
  assert.equal(result.first, true);
  assert.equal(result.middle, true, 'middle loaded row must be reachable in the virtual window');
  assert.equal(result.last, true, 'last loaded row must be reachable from End');
  assert.equal(result.adds, result.removes, 'normal close/reopen must not leak lifecycle listeners');
  console.log(JSON.stringify({passed:'S03 picker 5,001-row virtual window and keyboard reachability', ...result}));
});
