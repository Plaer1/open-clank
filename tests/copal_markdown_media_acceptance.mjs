import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const source = fs.readFileSync('static/js/copal.js', 'utf8');
const overrides = { '/static/js/copal.js':source + '\nexport const mediaFixture = { state, renderMarkdown };\n' };
const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><main id="render"></main><script type="module">
import { mediaFixture } from '/static/js/copal.js';
window.fixture=mediaFixture; window.errors=[]; window.addEventListener('unhandledrejection',e=>errors.push(String(e.reason)));
const origin={id:'origin',name:'Notes/旅行.md',kind:'markdown',text:''};
fixture.state.docs=[origin,{id:'photo',name:'Notes/photo one.png',kind:'asset'},{id:'duplicate',name:'Other/photo one.png',kind:'asset'},{id:'retry',name:'Notes/retry.png',kind:'asset'},
{id:'audio',name:'Notes/voice.wav',kind:'asset'},{id:'video',name:'Notes/clip.webm',kind:'asset'},{id:'pdf',name:'Notes/manual.pdf',kind:'asset'},
{id:'guide',name:'Notes/Guide.md',kind:'markdown',text:'# Kept\\nWanted content\\n# Omitted\\nOther content'},
{id:'cycle',name:'Notes/Cycle.md',kind:'markdown',text:'![[Cycle]]'}];
window.render=text=>document.getElementById('render').replaceChildren(fixture.renderMarkdown(text,new Set(['origin'])));
</script>`;
const delivered = [];
let retryAvailable = false;
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');
const request = async(req,res) => {
  const url = new URL(req.url,'http://fixture');
  if(url.pathname === '/') {res.setHeader('set-cookie','fixture-session=authorized; Path=/; SameSite=Lax');return false;}
  if(url.pathname === '/api/copal/assets/photo') {
    if(!req.headers.cookie?.includes('fixture-session=authorized') || url.searchParams.get('workspace') !== 'default') {res.writeHead(403);res.end();return true;}
    delivered.push(url.pathname);res.setHeader('content-type','image/png');res.end(png);return true;
  }
  if(url.pathname === '/remote-image') {delivered.push(url.pathname);res.setHeader('content-type','image/png');res.end(png);return true;}
  if(url.pathname === '/api/copal/assets/retry' && retryAvailable) {res.setHeader('content-type','image/png');res.end(png);return true;}
  if(['/api/copal/assets/audio','/api/copal/assets/video'].includes(url.pathname)) {
    // Keep metadata pending: this case checks viewer dispatch, not decoding.
    res.writeHead(200,{'content-type':url.pathname.endsWith('audio')?'audio/wav':'video/webm'});res.flushHeaders();return true;
  }
  if(url.pathname.startsWith('/api/')) {res.writeHead(404);res.end();return true;}
  return false;
};
await withCopalBrowser({page,overrides,request},async({evaluate,until,url,cdp})=>{
  // The fixture page has a <main id="render"> element. Browsers therefore
  // expose `window.render` as that element before the module installs the
  // callable renderer, making a truthiness wait a readiness false positive.
  await until('typeof window.render === "function"');
  await evaluate(`window.render(${JSON.stringify('before ![](./photo%20one.png) after\n![[photo one.png|120x60]] trailing text\n![Remote](') } + ${JSON.stringify(url + 'remote-image)')})`);
  await until('[...document.querySelectorAll("#render img")].length===3 && [...document.querySelectorAll("#render img")].every(img=>img.complete&&img.naturalWidth>0)');
  assert.match(await evaluate('document.getElementById("render").textContent'), /before.*after/);
  assert.match(await evaluate('document.getElementById("render").textContent'), /trailing text/);
  assert.deepEqual(await evaluate('(()=>{const r=document.querySelectorAll("#render img")[1].getBoundingClientRect();return {width:r.width,height:r.height};})()'), {width:120,height:60}, 'requested size measures the displayed border box');
  assert(delivered.includes('/api/copal/assets/photo'));
  assert(delivered.includes('/remote-image'));
  await evaluate(`window.render('![[Guide#Kept]] after section\\n![[Cycle]]')`);
  const section = await evaluate('document.getElementById("render").textContent');
  assert.match(section,/Wanted content/);assert(!section.includes('Other content'));assert.match(section,/after section/);assert.match(section,/Embed cycle/);
  await evaluate(`window.render('![[voice.wav]]\\n![[clip.webm]]\\n![[manual.pdf]]')`);
  assert.deepEqual(await evaluate('[...document.querySelectorAll("#render audio,#render video,#render iframe")].map(n=>n.tagName)'), ['AUDIO','VIDEO','IFRAME']);
  await evaluate(`window.render('![[retry.png]]')`);
  await until('Boolean(document.querySelector("#render [data-reference-status=error]"))');
  retryAvailable = true;
  await evaluate('document.querySelector("#render button").click()');
  await until('Boolean(document.querySelector("#render img")?.naturalWidth)');
  await evaluate(`window.render('![](file:///tmp/private.png)')`);
  assert.equal(await evaluate('document.querySelector("#render [data-reference-status]").dataset.referenceStatus'), 'unsupported');
  assert.deepEqual(await evaluate('window.errors'),[]);
  console.log(JSON.stringify({passed:['production relative and remote images decoded through HTTP', 'fixture cookie and workspace checked for managed delivery', 'sizing and trailing prose', 'section transclusion and cycles', 'MIME viewer dispatch (audio/video/PDF decoding not claimed)', 'failed image recovery with retry', 'unsupported local URL diagnostic'],browser:await cdp('Browser.getVersion')},null,2));
});
