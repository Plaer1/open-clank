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
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, 45_000);
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
  let reloadSequence = 0;
  const reloadAndWait = async (expression, label) => {
    const marker = `reload-${++reloadSequence}`;
    await evaluate(`window.__clankerAcceptanceReload=${JSON.stringify(marker)}`);
    await command('Page.reload', { ignoreCache:true });
    await waitFor(`window.__clankerAcceptanceReload!==${JSON.stringify(marker)} && (${expression})`, label);
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
  const canvasState = id => evaluate(`(() => {
    const canvas=document.getElementById(${JSON.stringify(id)});
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
  const canvasChange = (id, delay = 260) => evaluate(`(async () => {
    const canvas=document.getElementById(${JSON.stringify(id)});
    if (!canvas || !canvas.width || !canvas.height) return null;
    const ctx=canvas.getContext('2d');
    const before=ctx.getImageData(0,0,canvas.width,canvas.height).data;
    await new Promise(resolve => setTimeout(resolve, ${Number(delay)}));
    const after=ctx.getImageData(0,0,canvas.width,canvas.height).data;
    let changed=0, painted=0, sampled=0;
    for (let i=0; i<before.length; i+=16) {
      sampled+=1;
      const visible=before[i+3] || after[i+3];
      if (!visible) continue;
      painted+=1;
      if (Math.abs(before[i]-after[i]) + Math.abs(before[i+1]-after[i+1]) + Math.abs(before[i+2]-after[i+2]) + Math.abs(before[i+3]-after[i+3]) > 8) changed+=1;
    }
    return { changed, painted, sampled, ratio:painted ? changed/painted : 0, coverage:sampled ? changed/sampled : 0 };
  })()`);
  const canvasCadence = (id, duration = 260) => evaluate(`(async () => {
    const canvas=document.getElementById(${JSON.stringify(id)});
    if (!canvas) return null;
    const proto=CanvasRenderingContext2D.prototype;
    const clearRect=proto.clearRect;
    const stamps=[];
    proto.clearRect=function(...args) {
      if (this.canvas===canvas) stamps.push(performance.now());
      return clearRect.apply(this,args);
    };
    try { await new Promise(resolve=>setTimeout(resolve, ${Number(duration)})); }
    finally { proto.clearRect=clearRect; }
    const intervals=stamps.slice(1).map((stamp,index)=>stamp-stamps[index]).sort((a,b)=>a-b);
    return { paints:stamps.length, median:intervals[Math.floor(intervals.length/2)] || 0, max:intervals.at(-1) || 0 };
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
      favicon:decodeURIComponent(document.querySelector("link[rel='icon']").href.split(',')[1]),
      projectMark:document.querySelector('.welcome-name svg')?.innerHTML,
      liga:document.fonts.check("16px 'Liga Comic Mono'"), fredoka:document.fonts.check("32px 'Fredoka'"),
      sidebarTexture:getComputedStyle(document.querySelector('.sidebar')).backgroundImage,
      inputShadow:getComputedStyle(document.querySelector('.chat-input-bar')).boxShadow,
      sendBorder:getComputedStyle(document.querySelector('.send-btn')).borderTopWidth,
    };
  })()`);
  assert.deepEqual(dark.order, ['clanker-dark', 'clanker-light', 'dark']);
  assert.equal(dark.active, 'clanker-dark');
  assert(dark.classes.includes('theme-clanker-dark') && dark.classes.includes('bg-pattern-clanker-routefield'));
  assert.equal(dark.bg.toUpperCase(), '#191A1E');
  assert.match(dark.bodyFont, /Liga Comic Mono/); assert.match(dark.brandFont, /Fredoka/);
  assert.equal(dark.fontValue, 'liga-comic-mono'); assert.equal(dark.fontLocked, true);
  assert.equal(dark.routeMotion, 'active'); assert(dark.liga && dark.fredoka);
  assert.match(dark.favicon, /M16 3 29 27H3Z/); assert.doesNotMatch(dark.favicon, /M16 4L16 22L6 22Z/);
  assert.match(dark.projectMark, /M8\.5 17Q16 7 23\.5 17/);
  assert.equal(dark.sidebarTexture, 'none'); assert.doesNotMatch(dark.sidebarTexture, /url\(/);
  assert.notEqual(dark.inputShadow, 'none'); assert.equal(dark.sendBorder, '2px');
  const darkFrameA = await canvasState('clanker-routefield-canvas');
  await new Promise(resolve => setTimeout(resolve, 260));
  const darkFrameB = await canvasState('clanker-routefield-canvas');
  assert(darkFrameA?.painted > 0); assert.notEqual(darkFrameA.hash, darkFrameB?.hash);
  assert(darkFrameA.painted >= 20000, `route field only painted ${darkFrameA.painted} sampled pixels`);
  const routeStability = await canvasChange('clanker-routefield-canvas', 320);
  assert(routeStability?.changed > 0); assert(routeStability.ratio < 0.08, `route field changed ${Math.round(routeStability.ratio * 100)}% of painted samples`);
  const routeCadence = await canvasCadence('clanker-routefield-canvas');
  assert(routeCadence?.paints >= 10, `route field only painted ${routeCadence?.paints || 0} frames`);
  assert(routeCadence.median < 24, `route field median frame interval was ${routeCadence.median.toFixed(1)}ms`);
  await screenshot('clanker-dark-page');
  await screenshot('clanker-dark', 'popup');

  const patternResults = {};
  for (const [pattern, canvasId, screenshotName, minimumPainted] of [
    ['clanker-kene-weave', 'clanker-kene-weave-canvas', 'clanker-kene-weave', 70000],
    ['clanker-radar', 'clanker-radar-canvas', 'clanker-radar', 120000],
    ['clanker-gem-drift', 'clanker-gem-drift-canvas', 'clanker-gem-drift', 12000],
  ]) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change', {bubbles:true})); return select.value; })()`);
    await waitFor(`document.body.classList.contains('bg-pattern-${pattern}') && document.getElementById('${canvasId}')?.dataset.motion === 'active'`, pattern);
    const frameA = await canvasState(canvasId);
    await new Promise(resolve => setTimeout(resolve, 320));
    const frameB = await canvasState(canvasId);
    assert(frameA?.painted > 0, `${pattern} did not paint`);
    assert(frameA.painted >= minimumPainted, `${pattern} only painted ${frameA.painted} sampled pixels`);
    assert.notEqual(frameA.hash, frameB?.hash, `${pattern} did not animate`);
    patternResults[pattern] = { frameA, frameB };
    await screenshot(screenshotName);
  }

  const canvasPatternIds = {
    'clanker-routefield':'clanker-routefield-canvas',
    'clanker-kene-weave':'clanker-kene-weave-canvas',
    'clanker-radar':'clanker-radar-canvas',
    'clanker-gem-drift':'clanker-gem-drift-canvas',
    synapse:'synapse-canvas', rain:'rain-canvas', constellations:'constellations-canvas',
    'perlin-flow':'perlin-flow-canvas', petals:'petals-canvas', sparkles:'sparkles-canvas', embers:'embers-canvas',
  };
  const patternOrder = [
    'none', 'clanker-routefield', 'clanker-kene-weave', 'clanker-radar',
    'clanker-gem-drift', 'clanker-blueprint', 'dots', 'synapse', 'rain',
    'constellations', 'perlin-flow', 'petals', 'sparkles', 'embers',
  ];
  assert.deepEqual(await evaluate("[...document.getElementById('theme-bg-pattern-select').options].map(option => option.value)"), patternOrder);
  const transitionMatrix = await evaluate(`(async () => {
    const patterns=${JSON.stringify(patternOrder)};
    const canvasIds=${JSON.stringify(canvasPatternIds)};
    const select=document.getElementById('theme-bg-pattern-select');
    const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
    const failures=[];
    let checked=0;
    for (const from of patterns) {
      for (const to of patterns) {
        select.value=from;
        select.dispatchEvent(new Event('change',{bubbles:true}));
        await pause(18);
        select.value=to;
        select.dispatchEvent(new Event('change',{bubbles:true}));
        await pause(38);
        const canvases=[...document.querySelectorAll('[data-background-effect-canvas]')];
        const classes=[...document.body.classList].filter(name=>name.startsWith('bg-pattern-'));
        const expectedClass=to==='none' ? [] : ['bg-pattern-'+to];
        const expectedCanvas=canvasIds[to] || null;
        const background=getComputedStyle(document.body);
        const valid=classes.length===expectedClass.length
          && classes.every((name,index)=>name===expectedClass[index])
          && canvases.length===(expectedCanvas ? 1 : 0)
          && (!expectedCanvas || (canvases[0].id===expectedCanvas && canvases[0].dataset.motion==='active'
            && background.backgroundImage==='none' && background.animationName==='none'));
        if (!valid) failures.push({from,to,classes,canvases:canvases.map(node=>({id:node.id,motion:node.dataset.motion})),backgroundImage:background.backgroundImage,animationName:background.animationName});
        checked+=1;
      }
    }
    return {checked,failures};
  })()`);
  assert.equal(transitionMatrix.checked, patternOrder.length ** 2);
  assert.deepEqual(transitionMatrix.failures, []);

  const effectMotionResults = {};
  for (const [pattern, canvasId] of Object.entries(canvasPatternIds)) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.dataset.motion === 'active'`, `${pattern} canvas owner`);
    await new Promise(resolve => setTimeout(resolve, 420));
    const frameA = await canvasState(canvasId);
    await new Promise(resolve => setTimeout(resolve, 360));
    const frameB = await canvasState(canvasId);
    const change = await canvasChange(canvasId, 360);
    assert(frameA?.painted > 0, `${pattern} did not paint`);
    assert.notEqual(frameA.hash, frameB?.hash, `${pattern} did not animate`);
    assert(change?.coverage < 0.08, `${pattern} changed ${Math.round((change?.coverage || 0) * 100)}% of sampled pixels`);
    effectMotionResults[pattern] = { frameA, frameB, change };
  }

  await evaluate("document.querySelector('#themeGrid [data-theme=\"clanker-light\"]').click()");
  await waitFor("document.body.classList.contains('theme-clanker-light')", 'Clanker Light selection');
  const light = await evaluate(`(() => { const root=getComputedStyle(document.documentElement),body=getComputedStyle(document.body),saved=JSON.parse(localStorage.getItem('odysseus-theme')); return { bg:root.getPropertyValue('--bg').trim(), classes:[...document.body.classList], font:body.fontFamily, animation:body.animationName, saved, texture:getComputedStyle(document.querySelector('.sidebar')).backgroundImage }; })()`);
  assert.equal(light.bg.toUpperCase(), '#F3EEDB');
  assert(light.classes.includes('bg-pattern-clanker-blueprint'));
  assert.match(light.font, /Liga Comic Mono/); assert.match(light.animation, /clanker-lcars-status-sweep/);
  assert.equal(light.saved.name, 'clanker-light'); assert.equal(light.saved.font, 'liga-comic-mono');
  assert.equal(light.saved.bgPattern, 'clanker-blueprint'); assert.equal(light.texture, 'none'); assert.doesNotMatch(light.texture, /url\(/);
  await screenshot('clanker-light');

  await reloadAndWait("document.querySelector('#themeGrid .theme-swatch.active')?.dataset.theme === 'clanker-light'", 'Clanker Light reload persistence');
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
  await reloadAndWait("document.querySelector('#themeGrid .theme-swatch.active')?.dataset.theme === 'clanker-dark' && document.getElementById('clanker-routefield-canvas')", 'legacy Clanker migration');
  const migration = await evaluate(`(() => { const saved=JSON.parse(localStorage.getItem('odysseus-theme')); return {saved,bg:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),classes:[...document.body.classList]}; })()`);
  assert.equal(migration.bg.toUpperCase(), '#191A1E');
  assert.equal(migration.saved.colors.bg.toUpperCase(), '#191A1E');
  assert.equal(migration.saved.bgPattern, 'clanker-routefield');
  assert.equal(migration.saved.bgEffectColor.toUpperCase(), '#62C7E8');
  assert.equal(migration.saved.bgEffectIntensity, 0.64);

  await command('Emulation.setEmulatedMedia', { features:[{ name:'prefers-reduced-motion', value:'reduce' }] });
  await waitFor("!!document.querySelector('#themeGrid [data-theme=\"clanker-dark\"]')", 'Clanker Dark swatch');
  assert.equal(await evaluate("(() => { const sw=document.querySelector('#themeGrid [data-theme=\"clanker-dark\"]'); if (!sw) return false; sw.click(); return true; })()"), true);
  const reducedResults = {};
  for (const [pattern, canvasId] of Object.entries(canvasPatternIds)) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.dataset.motion === 'reduced'`, `${pattern} reduced motion`);
    const frameA = await canvasState(canvasId);
    await new Promise(resolve => setTimeout(resolve, 180));
    const frameB = await canvasState(canvasId);
    assert(frameA?.painted > 0, `${pattern} reduced frame did not paint`);
    assert.equal(frameA.hash, frameB?.hash, `${pattern} moved with reduced motion`);
    reducedResults[pattern] = frameA;
  }
  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-blueprint'; select.dispatchEvent(new Event('change',{bubbles:true})); })()");
  assert.equal(await evaluate("getComputedStyle(document.body).animationName"), 'none');
  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-routefield'; select.dispatchEvent(new Event('change',{bubbles:true})); })()");
  await waitFor("document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'reduced'", 'restored reduced route field');
  await command('Emulation.setEmulatedMedia', { features:[] });

  await command('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:1, mobile:true });
  await reloadAndWait("document.readyState === 'complete' && innerWidth === 390 && document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'mobile route field');
  const mobile = await evaluate(`(() => { const canvas=document.getElementById('clanker-routefield-canvas'); return { innerWidth, scrollWidth:document.documentElement.scrollWidth, canvasWidth:canvas?.width, canvasHeight:canvas?.height, classes:[...document.body.classList] }; })()`);
  assert.equal(mobile.scrollWidth, mobile.innerWidth); assert.equal(mobile.canvasWidth, 390); assert.equal(mobile.canvasHeight, 844);
  assert(mobile.classes.includes('bg-pattern-clanker-routefield'));
  await screenshot('clanker-dark-mobile');
  const mobilePatternResults = {};
  for (const [pattern, canvasId] of Object.entries(canvasPatternIds).filter(([name]) => name.startsWith('clanker-'))) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.dataset.motion === 'active'`, `${pattern} mobile canvas`);
    const frame = await canvasState(canvasId);
    assert.equal(frame?.width, 390, `${pattern} mobile width`);
    assert.equal(frame?.height, 844, `${pattern} mobile height`);
    assert(frame.painted > 0, `${pattern} mobile canvas was blank`);
    mobilePatternResults[pattern] = frame;
    await screenshot(`${pattern}-mobile`);
  }

  await command('Emulation.setDeviceMetricsOverride', { width:1440, height:1000, deviceScaleFactor:1, mobile:false });
  await evaluate("document.querySelector('#themeGrid [data-theme=\"clanker-light\"]').click()");
  await waitFor("JSON.parse(localStorage.getItem('odysseus-theme'))?.name === 'clanker-light'", 'saved light theme before login');
  await command('Page.navigate', { url:`${base}/login` });
  await waitFor("document.readyState === 'complete' && document.body.classList.contains('theme-clanker-dark') && document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'Clanker login theme');
  const login = await evaluate(`(async () => { await document.fonts.load("16px 'Liga Comic Mono'"); await document.fonts.load("32px 'Fredoka'"); const root=getComputedStyle(document.documentElement), body=getComputedStyle(document.body), card=getComputedStyle(document.querySelector('.card')); return { bg:root.getPropertyValue('--bg').trim(), savedName:JSON.parse(localStorage.getItem('odysseus-theme'))?.name, classes:[...document.body.classList], font:body.fontFamily, backgroundImage:body.backgroundImage, effectCanvasCount:document.querySelectorAll('[data-background-effect-canvas]').length, logoFont:getComputedStyle(document.querySelector('.logo span')).fontFamily, logoMark:document.querySelector('.logo-mark')?.innerHTML, favicon:decodeURIComponent(document.querySelector("link[rel='icon']").href.split(',')[1]), routeMotion:document.getElementById('clanker-routefield-canvas')?.dataset.motion, cardBorder:card.borderTopWidth, cardRadius:card.borderTopLeftRadius, cardShadow:card.boxShadow, liga:document.fonts.check("16px 'Liga Comic Mono'"), fredoka:document.fonts.check("32px 'Fredoka'") }; })()`);
  assert.match(login.font, /Liga Comic Mono/); assert.match(login.logoFont, /Fredoka/);
  assert.equal(login.bg.toUpperCase(), '#191A1E'); assert.equal(login.savedName, 'clanker-light');
  assert(login.classes.includes('theme-clanker-dark') && login.classes.includes('bg-pattern-clanker-routefield'));
  assert.equal(login.backgroundImage, 'none'); assert.equal(login.effectCanvasCount, 1);
  assert.equal(login.routeMotion, 'active'); assert.equal(login.cardBorder, '2px'); assert.equal(login.cardRadius, '16px');
  assert.match(login.favicon, /M16 3 29 27H3Z/); assert.match(login.logoMark, /M8\.5 17Q16 7 23\.5 17/);
  assert.notEqual(login.cardShadow, 'none'); assert(login.liga && login.fredoka);
  const loginFrameA = await canvasState('clanker-routefield-canvas');
  await new Promise(resolve => setTimeout(resolve, 260));
  const loginFrameB = await canvasState('clanker-routefield-canvas');
  assert(loginFrameA?.painted > 0); assert.notEqual(loginFrameA.hash, loginFrameB?.hash);
  await screenshot('clanker-login');
  const loginClip = await evaluate(`(() => { const r=document.querySelector('.card').getBoundingClientRect(); return {x:r.left,y:r.top,width:r.width,height:r.height,scale:1}; })()`);
  const loginCapture = await command('Page.captureScreenshot', { format:'png', clip:loginClip, captureBeyondViewport:false });
  fs.writeFileSync(path.join(outputDir, 'clanker-login-card.png'), Buffer.from(loginCapture.data, 'base64'));

  await command('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:1, mobile:true });
  await reloadAndWait("document.readyState === 'complete' && innerWidth === 390 && document.body.classList.contains('theme-clanker-dark') && document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'mobile dark login');
  const mobileLogin = await evaluate(`(() => { const rect=document.querySelector('.card').getBoundingClientRect(); return {overflow:document.documentElement.scrollWidth-window.innerWidth,left:rect.left,right:rect.right,viewport:window.innerWidth}; })()`);
  assert(mobileLogin.overflow <= 0); assert(mobileLogin.left >= 0); assert(mobileLogin.right <= mobileLogin.viewport);
  await screenshot('clanker-login-mobile');
  assert.deepEqual(exceptions, []);
  process.stdout.write(`${JSON.stringify({ dark, routeStability, routeCadence, patternResults, transitionMatrix, effectMotionResults, light, original, fontViews, migration, reducedResults, mobile, mobilePatternResults, login, mobileLogin, screenshots:outputDir }, null, 2)}\n`);
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => chromium.once('exit', resolve));
  fs.rmSync(profile, { recursive:true, force:true });
}
