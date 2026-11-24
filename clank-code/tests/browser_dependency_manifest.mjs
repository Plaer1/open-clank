#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const html = `<!doctype html>
<link rel="stylesheet" href="styles/main.css?v=42">
<img src="img/%E9%9B%AA%20space.png?cache=2">
<img src="img/Cafe%CC%81.png">
<script src="https://example.invalid/remote.js"></script>
<script type="module" src="scripts/module.js"></script>
<img src="../../outside.png">
<img src="img%2Fambiguous.png">`;
const css = new Map([
  ['ref-main-css', `@import url("./palette.css?theme=dark"); @import url("./palette theme.css?theme=dark"); .hero{background:url("../img/%E9%9B%AA%20space.png?rev=3")}`],
  ['ref-palette-css', `.icon{background-image:url('../img/雪%20space.png#icon')}`],
  ['ref-spaced-css', `.other{background:url("../img/Cafe%CC%81.png")}`],
]);
const htmlLiteral = JSON.stringify(html).replace(/<\/script/gi, '<\\/script');
const rows = {
  root: [
    { name:'styles', resource_ref:'ref-styles', kind:'directory', capabilities:['children'] },
    { name:'img', resource_ref:'ref-img', kind:'directory', capabilities:['children'] },
    { name:'scripts', resource_ref:'ref-scripts', kind:'directory', capabilities:['children'] },
    { name:'unused-secret.js', resource_ref:'ref-unused', kind:'file', capabilities:['preview','stat'], mime_type:'text/javascript', revision:{kind:'hostFingerprint',value:'unused-r1'}, size:8 },
  ],
  'ref-styles': [
    { name:'main.css', resource_ref:'ref-main-css', kind:'file', capabilities:['preview','stat'], mime_type:'text/css', revision:{kind:'hostFingerprint',value:'main-r1'}, size:100 },
    { name:'palette.css', resource_ref:'ref-palette-css', kind:'file', capabilities:['preview','stat'], mime_type:'text/css', revision:{kind:'hostFingerprint',value:'palette-r1'}, size:80 },
    { name:'palette theme.css', resource_ref:'ref-spaced-css', kind:'file', capabilities:['preview','stat'], mime_type:'text/css', revision:{kind:'hostFingerprint',value:'spaced-r1'}, size:90 },
  ],
  'ref-img': [
    { name:'雪 space.png', resource_ref:'ref-image', kind:'file', capabilities:['preview','stat'], mime_type:'image/png', revision:{kind:'hostFingerprint',value:'image-r1'}, size:40 },
    { name:'Café.png', resource_ref:'ref-composed-image', kind:'file', capabilities:['preview','stat'], mime_type:'image/png', revision:{kind:'hostFingerprint',value:'composed-r1'}, size:41 },
  ],
  'ref-scripts': [{ name:'module.js', resource_ref:'ref-module', kind:'file', capabilities:['preview','stat'], mime_type:'text/javascript', revision:{kind:'hostFingerprint',value:'module-r1'}, size:30 }],
};
const rowsLiteral = JSON.stringify(rows);
const cssLiteral = JSON.stringify(Object.fromEntries(css));

const page = `<!doctype html><meta charset="utf-8"><script type="module">
  import { acquirePreviewDependencies } from '/static/js/browser/dependencyManifest.js';
  import { createDependencyAcquisitionGuard, isDirectPreviewChild } from '/static/js/browser/dependencyManifest.js';
  const childrenCalls = [], statCalls = [], readCalls = [];
  const rows = ${rowsLiteral};
  const css = new Map(Object.entries(${cssLiteral}));
  const client = {
    async children(parent, options) {
      childrenCalls.push({parent,query:options.query});
      const query=String(options.query || '').toLocaleLowerCase();
      const entries=(rows[parent] || []).filter(entry=>!query || String(entry.name).toLocaleLowerCase().includes(query));
      return {entries,next_cursor:null};
    },
    async stat(ref) {
      statCalls.push(ref);
      const entry = Object.values(rows).flat().find(item => item.resource_ref === ref);
      return { resource:entry };
    },
  };
  window.acquireDependencies = acquirePreviewDependencies;
  window.createDependencyGuard = createDependencyAcquisitionGuard;
  window.isDirectPreviewChild = isDirectPreviewChild;
  window.testFilesClient = client;
  window.resultPromise = acquirePreviewDependencies({ html:${htmlLiteral}, rootRef:'root', client,
    readText:async ref => { readCalls.push(ref); return css.get(ref); },
  }).then(result => ({result,childrenCalls,statCalls,readCalls}));
</script>`;

await withCopalBrowser({ page }, async ({ until, evaluate }) => {
  await until('Boolean(window.resultPromise)');
  const evidence = await evaluate('window.resultPromise');
  const paths = evidence.result.dependencies.map(item => item.path).sort();
  assert.deepEqual(paths, ['img/Cafe\u0301.png','img/雪 space.png','scripts/module.js','styles/main.css','styles/palette.css','styles/palette theme.css'].sort());
  assert.ok(paths.includes('img/Cafe\u0301.png'), 'manifest key preserves decomposed URL spelling for the origin server');
  assert.ok(paths.includes('styles/palette theme.css'), 'quoted CSS @import accepts ordinary spaces');
  assert.equal(evidence.result.dependencies.length, 6, 'query strings and fragments do not duplicate the same immutable path');
  assert.ok(evidence.result.warnings.some(value => value.includes('External resources')));
  assert.ok(evidence.result.warnings.some(value => value.includes('escapes the selected Files folder')));
  assert.ok(evidence.result.warnings.some(value => value.includes('ambiguous URL path')));
  assert.ok(evidence.result.warnings.some(value => value.includes('module imports')));
  assert.equal(evidence.statCalls.includes('ref-unused'), false, 'unreferenced siblings are never read or statted');
  assert.deepEqual(evidence.readCalls.sort(), ['ref-main-css','ref-palette-css','ref-spaced-css']);
  assert.equal(evidence.childrenCalls.filter(item => item.parent === 'root' && item.query === 'img').length, 1,
    'directory segment lookups are reused across HTML and nested CSS references');
  assert.ok(evidence.childrenCalls.length <= 10, 'lookup follows only referenced path segments, including Unicode normalization variants');

  const baseResult = await evaluate(`window.acquireDependencies({html:'<base href="https://example.invalid/"><img src="asset.png">',rootRef:'root',client:window.testFilesClient,readText:async()=>''})`);
  assert.equal(baseResult.dependencies.length, 0, 'a base URL stops manifest acquisition instead of misresolving resources');
  assert.ok(baseResult.warnings.some(value => value.includes('base URL is unsupported')));

  const incomplete = await evaluate(`window.acquireDependencies({html:'<img src="img/雪%20space.png">',rootRef:'root',client:{
    async children(parent,options){return {entries:[],next_cursor:'cursor-' + (options.cursor || 'first')}} ,
    async stat(){throw new Error('stat must not run for an incomplete search')}
  },readText:async()=>''})`);
  assert.equal(incomplete.dependencies.length, 0, 'a lookup that remains paginated at the cap is not treated as complete');
  assert.ok(incomplete.warnings.some(value => value.includes('exceeded its page limit')));

  const guard = await evaluate(`(async()=>{const value=window.createDependencyGuard({timeoutMs:30});await new Promise(resolve=>value.signal.addEventListener('abort',resolve,{once:true}));const result={timedOut:value.timedOut,aborted:value.signal.aborted};value.dispose();return result})()`);
  assert.deepEqual(guard, {timedOut:true,aborted:true}, 'the overall dependency deadline aborts pending Files work');
  const invalidation = await evaluate(`(async()=>{let current=true;const value=window.createDependencyGuard({isCurrent:()=>current,timeoutMs:1000});const pending=new Promise(resolve=>value.signal.addEventListener('abort',resolve,{once:true}));current=false;await pending;const result={timedOut:value.timedOut,aborted:value.signal.aborted};value.dispose();return result})()`);
  assert.deepEqual(invalidation, {timedOut:false,aborted:true}, 'selection retirement aborts in-flight acquisition before it can publish');
  const direct = await evaluate(`({column:window.isDirectPreviewChild({folderRef:'folder-ref'}),treeMatch:window.isDirectPreviewChild({folderRef:'folder-ref',entryParentRef:'folder-ref',treeEntry:true}),treeMismatch:window.isDirectPreviewChild({folderRef:'folder-ref',entryParentRef:'other-ref',treeEntry:true}),missingParent:window.isDirectPreviewChild({folderRef:'',treeEntry:true}),directory:window.isDirectPreviewChild({folderRef:'folder-ref',directory:true})})`);
  assert.deepEqual(direct, {column:true,treeMatch:true,treeMismatch:false,missingParent:false,directory:false});
});
