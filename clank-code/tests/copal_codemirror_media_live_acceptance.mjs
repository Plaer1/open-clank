#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><style>#editor{width:760px;height:420px}.cm-editor{height:100%}.cm-md-embed-widget img,.cm-md-inline-preview-widget img{max-width:180px;max-height:90px}</style><main id="editor"></main><script type="module">
import { createMarkdownEditor } from '/static/js/copal/codemirror.js';
const host=document.querySelector('#editor');
const source='before ![inline](/media.png) after\\n![[asset.png]] trailing prose\\n![[standalone.png]]\\n\\nend';
const renderPreview=(source)=>{const image=document.createElement('img');const markdown=/!\\[[^\\]]*\\]\\(([^)]+)\\)/.exec(source);const wiki=/!\\[\\[([^\\]|]+)(?:\\|[^\\]]+)?\\]\\]/.exec(source);image.alt='preview';image.src=markdown?.[1]||('/'+(wiki?.[1]||'missing.png'));return image;};
window.editor=createMarkdownEditor({parent:host,doc:source,selection:{anchor:source.length,head:source.length},renderPreview,onChange:(value)=>window.lastValue=value});
window.auditReady=true;
</script>`;
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=','base64');
const request=async(req,res)=>{const path=new URL(req.url,'http://fixture').pathname;if(path==='/media.png'||path==='/asset.png'||path==='/standalone.png'){res.setHeader('content-type','image/png');res.end(png);return true;}return false;};
const results=[];
await withCopalBrowser({page,request},async({evaluate,until})=>{
  await until('Boolean(window.auditReady)');
  await until('document.querySelectorAll("#editor img[alt=preview]").length===3');
  assert.equal(await evaluate('document.querySelectorAll("#editor img[alt=preview]").length'),3);
  assert.match(await evaluate('document.querySelector("#editor").textContent'),/trailing prose/);
  results.push('live inline and standalone Markdown/wiki image previews decode with trailing prose');
  const sourceText=await evaluate('window.editor.getValue()');
  await evaluate('window.editor.setMode("source")');
  await until('document.querySelector("#editor .cm-content")?.textContent.includes("![[asset.png]]")');
  assert.equal(await evaluate('window.editor.getValue()'),sourceText);
  results.push('source mode preserves editable syntax and document value');
  await evaluate('window.editor.setMode("live")');
  await until('document.querySelectorAll("#editor img[alt=preview]").length===3');
  await evaluate('document.querySelector("#editor .cm-md-inline-preview-widget")?.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}))');
  const selection=await evaluate('window.editor.getSelection()');
  assert.equal(selection.anchor,sourceText.indexOf('![inline]'));
  await evaluate(`window.editor.view.dispatch({selection:{anchor:window.editor.getValue().length,head:window.editor.getValue().length}}); document.querySelector('.cm-md-embed-widget img[src$="/asset.png"]')?.closest('.cm-md-embed-widget')?.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
  const wikiSelection=await evaluate('window.editor.getSelection()');
  assert.equal(wikiSelection.anchor,sourceText.indexOf('![[asset.png]]'));
  results.push('Enter and double-click on live previews reveal source selections');
  await evaluate('window.editor.view.dispatch({changes:{from:0,to:0,insert:"X"}})');
  assert.equal((await evaluate('window.editor.getValue()')).startsWith('Xbefore'),true);
  results.push('active source editing remains writable after live preview toggle');
});
console.log(JSON.stringify({passed:results},null,2));
