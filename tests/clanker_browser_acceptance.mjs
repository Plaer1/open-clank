#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7000').replace(/\/$/, '');
const outputDir = process.argv[3] || '/tmp/openclank-clanker-browser';
fs.mkdirSync(outputDir, { recursive:true });

const port = await new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-clanker-'));
const chromium = spawn('/usr/bin/chromium', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio:'ignore' });

let socket;
try {
  const debuggerBase = `http://127.0.0.1:${port}`;
  let targets;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { targets = await fetch(`${debuggerBase}/json`).then(response => response.json()); break; }
    catch { await new Promise(resolve => setTimeout(resolve, 50)); }
  }
  const target = targets?.find(item => item.type === 'page');
  assert(target?.webSocketDebuggerUrl, 'Chromium page target is unavailable');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once:true });
    socket.addEventListener('error', reject, { once:true });
  });

  let sequence = 0;
  const pending = new Map();
  const exceptions = [];
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id); if (!request) return;
      pending.delete(message.id); clearTimeout(request.timer);
      message.error ? request.reject(new Error(`${request.method}: ${message.error.message}`)) : request.resolve(message.result);
    } else if (message.method === 'Runtime.exceptionThrown') {
      const detail = message.params.exceptionDetails;
      if (/(?:\/static\/(?:index\.html|js\/theme\.js)|\/login)$/.test(detail.url || '')) {
        exceptions.push(detail.exception?.description || detail.text);
      }
    }
  });
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, 20_000);
    pending.set(id, { resolve, reject, timer, method });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const response = await command('Runtime.evaluate', { expression, awaitPromise:true, returnByValue:true });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    return response.result.value;
  };
  const waitFor = async (expression, label) => {
    const deadline = Date.now() + 20_000;
    while (Date.now() < deadline) {
      try { if (await evaluate(expression)) return; } catch {}
      await new Promise(resolve => setTimeout(resolve, 75));
    }
    throw new Error(`Timed out waiting for ${label}`);
  };
  const screenshot = async (name, scope = 'page') => {
    await evaluate(`(() => {
      document.getElementById('app-loader')?.remove();
      const modal=document.getElementById('theme-modal');
      if (modal) modal.classList.toggle('hidden', ${JSON.stringify(scope)} !== 'popup');
    })()`);
    await new Promise(resolve => setTimeout(resolve, 320));
    const params = { format:'png', captureBeyondViewport:false };
    if (scope === 'popup') {
      params.clip = await evaluate(`(() => { const r=document.getElementById('theme-popup').getBoundingClientRect(); return {x:r.left,y:r.top,width:r.width,height:r.height,scale:1}; })()`);
    }
    const capture = await command('Page.captureScreenshot', params);
    fs.writeFileSync(path.join(outputDir, `${name}.png`), Buffer.from(capture.data, 'base64'));
  };
  const routefieldState = () => evaluate(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    if (!canvas || !canvas.width || !canvas.height) return null;
    const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
    let hash=2166136261, painted=0;
    for (let i=0; i<data.length; i+=16) {
      hash=Math.imul(hash ^ data[i], 16777619);
      hash=Math.imul(hash ^ data[i+1], 16777619);
      hash=Math.imul(hash ^ data[i+2], 16777619);
      hash=Math.imul(hash ^ data[i+3], 16777619);
      if (data[i+3]) painted+=1;
    }
    return { hash:hash>>>0, painted, width:canvas.width, height:canvas.height, motion:canvas.dataset.motion };
  })()`);

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Network.enable');
  await command('Network.setCacheDisabled', { cacheDisabled:true });
  await command('Network.setBypassServiceWorker', { bypass:true });
  await command('Emulation.setDeviceMetricsOverride', { width:1440, height:1000, deviceScaleFactor:1, mobile:false });
  const preload = await command('Page.addScriptToEvaluateOnNewDocument', { source:`(() => {
    if (!sessionStorage.getItem('__clanker_fresh')) {
      localStorage.removeItem('odysseus-theme');
      localStorage.removeItem('odysseus-custom-themes');
      sessionStorage.setItem('__clanker_fresh', '1');
    }
    const realFetch = window.fetch.bind(window);
    window.fetch = (input, options) => {
      const url = new URL(String(input), location.href);
      if (!url.pathname.startsWith('/api/')) return realFetch(input, options);
      let body = '{}';
      if (url.pathname === '/api/auth/status') body = location.pathname === '/login'
        ? '{"configured":true,"authenticated":false,"username":null,"is_admin":false}'
        : '{"configured":true,"authenticated":true,"username":"theme-test","is_admin":true,"privileges":{}}';
      else if (url.pathname === '/api/prefs/theme') body = '{"value":null}';
      else if (url.pathname === '/api/prefs/custom-themes') body = '{"value":{}}';
      else if (url.pathname === '/api/sessions') body = '[]';
      else if (url.pathname === '/api/models') body = '{"items":[]}';
      return Promise.resolve(new Response(body, {status:200, headers:{'Content-Type':'application/json'}}));
    };
  })();` });
  await command('Page.navigate', { url:`${base}/static/index.html` });
  await waitFor("document.readyState === 'complete' && document.querySelectorAll('#themeGrid .theme-swatch').length >= 18", 'fresh theme UI');
  await waitFor("document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'active Clanker route field');

  const dark = await evaluate(`(async () => {
    await document.fonts.load("16px 'Liga Comic Mono'");
    await document.fonts.load("32px 'Fredoka'");
    const root=getComputedStyle(document.documentElement), body=getComputedStyle(document.body);
    return {
      order:[...document.querySelectorAll('#themeGrid .theme-swatch')].slice(0,3).map(node=>node.dataset.theme),
      active:document.querySelector('#themeGrid .theme-swatch.active')?.dataset.theme,
      classes:[...document.body.classList], bg:root.getPropertyValue('--bg').trim(),
      bodyFont:body.fontFamily, brandFont:getComputedStyle(document.querySelector('.sidebar-brand-title')).fontFamily,
      fontValue:document.getElementById('theme-font-select').value,
      fontLocked:document.getElementById('theme-font-select').disabled,
      routeMotion:document.getElementById('clanker-routefield-canvas')?.dataset.motion,
      liga:document.fonts.check("16px 'Liga Comic Mono'"), fredoka:document.fonts.check("32px 'Fredoka'"),
      sidebarTexture:getComputedStyle(document.querySelector('.sidebar')).backgroundImage,
      inputShadow:getComputedStyle(document.querySelector('.chat-input-bar')).boxShadow,
      sendBorder:getComputedStyle(document.querySelector('.send-btn')).borderTopWidth,
    };
  })()`);
  assert.deepEqual(dark.order, ['clanker-dark', 'clanker-light', 'dark']);
  assert.equal(dark.active, 'clanker-dark');
  assert(dark.classes.includes('theme-clanker-dark') && dark.classes.includes('bg-pattern-clanker-routefield'));
  assert.equal(dark.bg.toUpperCase(), '#101727');
  assert.match(dark.bodyFont, /Liga Comic Mono/); assert.match(dark.brandFont, /Fredoka/);
  assert.equal(dark.fontValue, 'liga-comic-mono'); assert.equal(dark.fontLocked, true);
  assert.equal(dark.routeMotion, 'active'); assert(dark.liga && dark.fredoka);
  assert.match(dark.sidebarTexture, /gradient/); assert.doesNotMatch(dark.sidebarTexture, /url\(/);
  assert.notEqual(dark.inputShadow, 'none'); assert.equal(dark.sendBorder, '2px');
  const darkFrameA = await routefieldState();
  await new Promise(resolve => setTimeout(resolve, 260));
  const darkFrameB = await routefieldState();
  assert(darkFrameA?.painted > 0); assert.notEqual(darkFrameA.hash, darkFrameB?.hash);
  await screenshot('clanker-dark-page');
  await screenshot('clanker-dark', 'popup');

  await evaluate("document.querySelector('#themeGrid [data-theme=\"clanker-light\"]').click()");
  await waitFor("document.body.classList.contains('theme-clanker-light')", 'Clanker Light selection');
  const light = await evaluate(`(() => { const root=getComputedStyle(document.documentElement),body=getComputedStyle(document.body),saved=JSON.parse(localStorage.getItem('odysseus-theme')); return { bg:root.getPropertyValue('--bg').trim(), classes:[...document.body.classList], font:body.fontFamily, animation:body.animationName, saved, texture:getComputedStyle(document.querySelector('.sidebar')).backgroundImage }; })()`);
  assert.equal(light.bg.toUpperCase(), '#F1ECD7');
  assert(light.classes.includes('bg-pattern-clanker-blueprint'));
  assert.match(light.font, /Liga Comic Mono/); assert.match(light.animation, /clanker-blueprint-conveyor/);
  assert.equal(light.saved.name, 'clanker-light'); assert.equal(light.saved.font, 'liga-comic-mono');
  assert.equal(light.saved.bgPattern, 'clanker-blueprint'); assert.match(light.texture, /cream-paper-pulp/);
  await screenshot('clanker-light');

  await command('Page.reload', { ignoreCache:true });
  await waitFor("document.querySelector('#themeGrid .theme-swatch.active')?.dataset.theme === 'clanker-light'", 'Clanker Light reload persistence');
  await waitFor("!!document.querySelector('#themeGrid [data-theme=\"dark\"]')", 'Original theme swatch');
  assert.equal(await evaluate("(() => { const sw=document.querySelector('#themeGrid [data-theme=\"dark\"]'); if (!sw) return false; sw.click(); return true; })()"), true);
  const original = await evaluate(`(() => ({ classes:[...document.body.classList], font:getComputedStyle(document.body).fontFamily, pattern:JSON.parse(localStorage.getItem('odysseus-theme')).bgPattern || 'none', locked:document.getElementById('theme-font-select').disabled }))()`);
  assert(!original.classes.some(name => name.startsWith('theme-clanker-')));
  assert.match(original.font, /Fira Code/); assert.equal(original.pattern, 'none'); assert.equal(original.locked, false);

  const fontViews = await evaluate(`(() => {
    const select=document.getElementById('theme-font-select');
    select.value='serif'; select.dispatchEvent(new Event('change', {bubbles:true}));
    const fixture=document.createElement('div');
    fixture.style.cssText='position:fixed;left:-10000px;top:0;display:block';
    fixture.innerHTML='<section class="modal-content" data-font-test="modal"><div class="notes-pane" data-font-test="notes"><h2 class="notes-pane-title" data-font-test="title">Notes</h2></div><div class="copal-workspace" data-font-test="copal"><article class="copal-note-live-preview" data-font-test="preview">Preview</article><div class="copal-codemirror-host" data-mode="source"><div class="cm-scroller" data-font-test="source">source</div></div></div></section>';
    document.body.appendChild(fixture);
    const font=(name)=>getComputedStyle(fixture.querySelector('[data-font-test="'+name+'"]')).fontFamily;
    const result={ root:getComputedStyle(document.body).fontFamily, modal:font('modal'), notes:font('notes'), title:font('title'), copal:font('copal'), preview:font('preview'), source:font('source') };
    fixture.remove();
    return result;
  })()`);
  for (const name of ['root','modal','notes','title','copal','preview']) assert.match(fontViews[name], /Georgia/);
  assert.doesNotMatch(fontViews.source, /Georgia/);

  await evaluate(`localStorage.setItem('odysseus-theme', JSON.stringify({
    name:'clanker-dark',
    colors:{bg:'#090D13',fg:'#F7F1D7',panel:'#111B27',border:'#2C70D6',red:'#55A2FF'},
    bgPattern:'clanker-sweep',bgEffectColor:'#78D4F3',bgEffectIntensity:0.7
  }))`);
  await command('Page.reload', { ignoreCache:true });
  await waitFor("document.querySelector('#themeGrid .theme-swatch.active')?.dataset.theme === 'clanker-dark' && document.getElementById('clanker-routefield-canvas')", 'legacy Clanker migration');
  const migration = await evaluate(`(() => { const saved=JSON.parse(localStorage.getItem('odysseus-theme')); return {saved,bg:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),classes:[...document.body.classList]}; })()`);
  assert.equal(migration.bg.toUpperCase(), '#101727');
  assert.equal(migration.saved.colors.bg.toUpperCase(), '#101727');
  assert.equal(migration.saved.bgPattern, 'clanker-routefield');
  assert.equal(migration.saved.bgEffectColor.toUpperCase(), '#5ABCF5');
  assert.equal(migration.saved.bgEffectIntensity, 0.8);

  await command('Emulation.setEmulatedMedia', { features:[{ name:'prefers-reduced-motion', value:'reduce' }] });
  await waitFor("!!document.querySelector('#themeGrid [data-theme=\"clanker-dark\"]')", 'Clanker Dark swatch');
  assert.equal(await evaluate("(() => { const sw=document.querySelector('#themeGrid [data-theme=\"clanker-dark\"]'); if (!sw) return false; sw.click(); return true; })()"), true);
  await waitFor("document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'reduced'", 'reduced-motion route field');
  const reducedFrameA = await routefieldState();
  await new Promise(resolve => setTimeout(resolve, 260));
  const reducedFrameB = await routefieldState();
  assert(reducedFrameA?.painted > 0); assert.equal(reducedFrameA.hash, reducedFrameB?.hash);
  await command('Emulation.setEmulatedMedia', { features:[] });

  await command('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:1, mobile:true });
  await command('Page.reload', { ignoreCache:true });
  await waitFor("document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'mobile route field');
  const mobile = await evaluate(`(() => { const canvas=document.getElementById('clanker-routefield-canvas'); return { innerWidth, scrollWidth:document.documentElement.scrollWidth, canvasWidth:canvas?.width, canvasHeight:canvas?.height, classes:[...document.body.classList] }; })()`);
  assert.equal(mobile.scrollWidth, mobile.innerWidth); assert.equal(mobile.canvasWidth, 390); assert.equal(mobile.canvasHeight, 844);
  assert(mobile.classes.includes('bg-pattern-clanker-routefield'));
  await screenshot('clanker-dark-mobile');

  await command('Emulation.setDeviceMetricsOverride', { width:1440, height:1000, deviceScaleFactor:1, mobile:false });
  await evaluate("document.querySelector('#themeGrid [data-theme=\"clanker-light\"]').click()");
  await waitFor("JSON.parse(localStorage.getItem('odysseus-theme'))?.name === 'clanker-light'", 'saved light theme before login');
  await command('Page.navigate', { url:`${base}/login` });
  await waitFor("document.readyState === 'complete' && document.body.classList.contains('theme-clanker-dark') && document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'Clanker login theme');
  const login = await evaluate(`(async () => { await document.fonts.load("16px 'Liga Comic Mono'"); await document.fonts.load("32px 'Fredoka'"); const root=getComputedStyle(document.documentElement), card=getComputedStyle(document.querySelector('.card')); return { bg:root.getPropertyValue('--bg').trim(), savedName:JSON.parse(localStorage.getItem('odysseus-theme'))?.name, classes:[...document.body.classList], font:getComputedStyle(document.body).fontFamily, logoFont:getComputedStyle(document.querySelector('.logo span')).fontFamily, routeMotion:document.getElementById('clanker-routefield-canvas')?.dataset.motion, cardBorder:card.borderTopWidth, cardRadius:card.borderTopLeftRadius, cardShadow:card.boxShadow, liga:document.fonts.check("16px 'Liga Comic Mono'"), fredoka:document.fonts.check("32px 'Fredoka'") }; })()`);
  assert.match(login.font, /Liga Comic Mono/); assert.match(login.logoFont, /Fredoka/);
  assert.equal(login.bg.toUpperCase(), '#101727'); assert.equal(login.savedName, 'clanker-light');
  assert(login.classes.includes('theme-clanker-dark') && login.classes.includes('bg-pattern-clanker-routefield'));
  assert.equal(login.routeMotion, 'active'); assert.equal(login.cardBorder, '2px'); assert.equal(login.cardRadius, '8px');
  assert.notEqual(login.cardShadow, 'none'); assert(login.liga && login.fredoka);
  const loginFrameA = await routefieldState();
  await new Promise(resolve => setTimeout(resolve, 260));
  const loginFrameB = await routefieldState();
  assert(loginFrameA?.painted > 0); assert.notEqual(loginFrameA.hash, loginFrameB?.hash);
  await screenshot('clanker-login');
  const loginClip = await evaluate(`(() => { const r=document.querySelector('.card').getBoundingClientRect(); return {x:r.left,y:r.top,width:r.width,height:r.height,scale:1}; })()`);
  const loginCapture = await command('Page.captureScreenshot', { format:'png', clip:loginClip, captureBeyondViewport:false });
  fs.writeFileSync(path.join(outputDir, 'clanker-login-card.png'), Buffer.from(loginCapture.data, 'base64'));

  await command('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:1, mobile:true });
  await command('Page.reload', { ignoreCache:true });
  await waitFor("document.body.classList.contains('theme-clanker-dark') && document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'mobile dark login');
  const mobileLogin = await evaluate(`(() => { const rect=document.querySelector('.card').getBoundingClientRect(); return {overflow:document.documentElement.scrollWidth-window.innerWidth,left:rect.left,right:rect.right,viewport:window.innerWidth}; })()`);
  assert(mobileLogin.overflow <= 0); assert(mobileLogin.left >= 0); assert(mobileLogin.right <= mobileLogin.viewport);
  await screenshot('clanker-login-mobile');
  assert.deepEqual(exceptions, []);
  process.stdout.write(`${JSON.stringify({ dark, light, original, fontViews, migration, reduced:reducedFrameA, mobile, login, mobileLogin, screenshots:outputDir }, null, 2)}\n`);
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => chromium.once('exit', resolve));
  fs.rmSync(profile, { recursive:true, force:true });
}
