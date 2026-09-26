#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7777').replace(/\/$/, '');
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
const chrome = [
  process.env.OPENCLANK_CHROME_BIN,
  process.env.OPEN_CLANK_CHROME_BIN,
  process.env.CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/opt/homebrew/bin/chromium',
  '/usr/local/bin/chromium',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/usr/bin/google-chrome',
].find(candidate => candidate && fs.existsSync(candidate));
assert(chrome, 'Chrome/Chromium executable required; set OPENCLANK_CHROME_BIN or CHROME_BIN');
const chromium = spawn(chrome, [
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
    const state = await evaluate(`(() => {
      const canvases = [...document.querySelectorAll('[data-background-effect-canvas]')];
      return {
        url: location.href,
        ready: document.readyState,
        viewport: [innerWidth, innerHeight],
        bodyClasses: [...document.body.classList],
        authOwner: localStorage.getItem('odysseus-auth-owner'),
        authUser: localStorage.getItem('odysseus-auth-user'),
        themeKeys: [...Array(localStorage.length)].map((_, index) => localStorage.key(index)).filter(key => key?.startsWith('odysseus-theme')),
        themeNames: [...Array(localStorage.length)].map((_, index) => localStorage.key(index)).filter(key => key?.startsWith('odysseus-theme')).map(key => {
          try { return [key, JSON.parse(localStorage.getItem(key) || 'null')?.name || null]; } catch { return [key, 'invalid']; }
        }),
        themeAuthPromise: !!window.__odysseusAuthContextPromise,
        canvases: canvases.map(canvas => ({
          id: canvas.id,
          width: canvas.width,
          height: canvas.height,
        })),
      };
    })()`).catch(error => ({ diagnosticError: String(error) }));
    throw new Error(`Timed out waiting for ${label}: ${JSON.stringify(state)}`);
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
    return { hash:hash>>>0, painted, width:canvas.width, height:canvas.height };
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
    return { paints:stamps.length, min:intervals[0] || 0, median:intervals[Math.floor(intervals.length/2)] || 0, max:intervals.at(-1) || 0 };
  })()`);
  const canvasCadenceStable = async id => {
    const cadence = await canvasCadence(id, 340);
    const minimumPaints = id.startsWith('clanker-') ? 6 : 2;
    assert(cadence?.paints >= minimumPaints, `${id} only painted ${cadence?.paints || 0} frames`);
    assert(cadence.min >= 7, `${id} rendered twice inside a single frame (${cadence.min.toFixed(1)}ms)`);
    return cadence;
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Network.enable');
  await command('Network.setCacheDisabled', { cacheDisabled:true });
  await command('Network.setBypassServiceWorker', { bypass:true });
  await command('Emulation.setDeviceMetricsOverride', { width:1440, height:1000, deviceScaleFactor:1, mobile:false });
  const preload = await command('Page.addScriptToEvaluateOnNewDocument', { source:`(() => {
    if (!sessionStorage.getItem('__clanker_fresh')) {
      const persistedTheme = {
        name:'clanker-dark',
        colors:{bg:'#191A1E',fg:'#FFF4D6',panel:'#25272C',border:'#555A62',red:'#5A9EF5'},
        bgPattern:'clanker-kene-weave',
        bgEffectColor:'#62C7E8',
        bgEffectIntensity:0.65,
        bgEffectSize:1,
        bgEffectControls:{
          'clanker-kene-weave':{
            snakeCount:23,
            snakeSpeed:80,
            snakeLengthVariation:85,
            shorterLastLonger:true,
            shorterLifetimeScale:135,
            longerDisappearSooner:true,
            longerLifetimeScale:65
          }
        }
      };
      // The authenticated boot contract reads the owner namespace before the
      // module loads. Seed that namespace so this startup gate tests the
      // persisted-account path instead of intentionally rejecting anonymous
      // state and waiting forever for Kene.
      localStorage.setItem('odysseus-auth-user', 'theme-test');
      localStorage.setItem('odysseus-auth-owner', 'theme-test');
      localStorage.setItem('odysseus-theme:scope:theme-test', JSON.stringify(persistedTheme));
      localStorage.setItem('odysseus-theme', JSON.stringify(persistedTheme));
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
  await waitFor("document.readyState === 'complete' && document.querySelectorAll('#themeGrid .theme-swatch').length >= 18 && document.getElementById('clanker-kene-weave-canvas')?.isConnected", 'persisted Signal Weave startup');
  const persistedStartup = await evaluate(`(async () => {
    const first=document.getElementById('clanker-kene-weave-canvas');
    const configurations=new Set();
    let stable=!!first;
    let canvasMutations=0;
    const observer=new MutationObserver(records => {
      for (const record of records) {
        for (const node of [...record.addedNodes, ...record.removedNodes]) {
          if (node instanceof HTMLCanvasElement && node.matches('[data-background-effect-canvas]')) canvasMutations+=1;
        }
      }
    });
    observer.observe(document.body,{childList:true});
    for (let sample=0; sample<80; sample+=1) {
      if (sample===20) await import('/static/js/theme.js?legacy-runtime=20260723gui1');
      const canvases=[...document.querySelectorAll('[data-background-effect-canvas]')];
      const canvas=document.getElementById('clanker-kene-weave-canvas');
      const styles=getComputedStyle(document.body);
      configurations.add(JSON.stringify({
        theme:[...document.body.classList].filter(name=>name.startsWith('theme-clanker-')),
        pattern:[...document.body.classList].filter(name=>name.startsWith('bg-pattern-')),
        colors:['--bg-effect-color','--clanker-gold','--clanker-lime','--clanker-pink','--clanker-coral','--clanker-lilac','--clanker-outline']
          .map(name=>styles.getPropertyValue(name).trim()),
        intensity:styles.getPropertyValue('--bg-effect-intensity').trim(),
        size:getComputedStyle(document.documentElement).getPropertyValue('--bg-effect-size').trim(),
      }));
      stable=stable
        && canvases.length===1
        && canvas===first
        && canvas?.isConnected;
      await new Promise(resolve=>setTimeout(resolve,50));
    }
    observer.disconnect();
    return {
      stable,
      canvasMutations,
      configurations:configurations.size,
    };
  })()`);
  assert.equal(persistedStartup.stable, true, 'persisted Signal Weave changed canvas, scene, class, or motion state');
  assert.equal(persistedStartup.canvasMutations, 0, 'a second theme module remounted the running vanilla-owned canvas');
  assert.equal(persistedStartup.configurations, 1, 'persisted Signal Weave palette or effect configuration oscillated');
  const paletteFallback = await evaluate(`(async () => {
    const canvas=document.getElementById('clanker-kene-weave-canvas');
    const staticCanvas=canvas?.__backgroundStaticCanvas;
    const proto=CanvasRenderingContext2D.prototype;
    const stroke=proto.stroke, fill=proto.fill;
    const styles=new Set();
    proto.stroke=function(...args) {
      if (this.canvas===canvas) styles.add(String(this.strokeStyle).toLowerCase());
      return stroke.apply(this,args);
    };
    proto.fill=function(...args) {
      if (this.canvas===canvas) styles.add(String(this.fillStyle).toLowerCase());
      return fill.apply(this,args);
    };
    try {
      document.body.classList.remove('theme-clanker-dark');
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    } finally {
      document.body.classList.add('theme-clanker-dark');
      proto.stroke=stroke;
      proto.fill=fill;
    }
    const pixels=staticCanvas?.getContext('2d')?.getImageData(0,0,staticCanvas.width,staticCanvas.height).data || [];
    const colors=new Set();
    for(let i=0;i<pixels.length;i+=4) if(pixels[i+3]) colors.add(pixels[i]+','+pixels[i+1]+','+pixels[i+2]);
    return {
      styles:[...styles],
      cachedColors:colors.size,
      stableCanvas:canvas?.isConnected,
    };
  })()`);
  assert(paletteFallback.styles.length >= 6 || paletteFallback.cachedColors >= 6, `Signal Weave collapsed to ${paletteFallback.styles.join(', ')} without the body theme class`);
  assert.equal(paletteFallback.stableCanvas, true, 'Signal Weave lost its canvas while preserving its palette');
  const vanillaPaletteFallback = await evaluate(`(async () => {
    const select=document.getElementById('theme-bg-pattern-select');
    select.value='synapse';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const canvas=document.getElementById('synapse-canvas');
    const proto=CanvasRenderingContext2D.prototype;
    const stroke=proto.stroke, fill=proto.fill;
    const styles=new Set();
    proto.stroke=function(...args) {
      if (this.canvas===canvas && typeof this.strokeStyle==='string') styles.add(this.strokeStyle.toLowerCase());
      return stroke.apply(this,args);
    };
    proto.fill=function(...args) {
      if (this.canvas===canvas && typeof this.fillStyle==='string') styles.add(this.fillStyle.toLowerCase());
      return fill.apply(this,args);
    };
    try {
      document.body.classList.remove('theme-clanker-dark');
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    } finally {
      document.body.classList.add('theme-clanker-dark');
      proto.stroke=stroke;
      proto.fill=fill;
    }
    return [...styles];
  })()`);
  assert(vanillaPaletteFallback.length >= 6, `Synapse collapsed to ${vanillaPaletteFallback.join(', ')} without the body theme class`);

  await evaluate("localStorage.removeItem('odysseus-theme'); localStorage.removeItem('odysseus-theme:scope:theme-test')");
  await reloadAndWait("document.readyState === 'complete' && document.querySelectorAll('#themeGrid .theme-swatch').length >= 18 && document.getElementById('clanker-routefield-canvas')?.isConnected", 'fresh theme UI');
  await waitFor("document.getElementById('clanker-routefield-canvas')?.isConnected", 'active Clanker route field');
  const themeModuleLoads = await evaluate(`performance.getEntriesByType('resource')
    .map(entry => new URL(entry.name))
    .filter(url => url.pathname === '/static/js/theme.js')
    .map(url => url.pathname + url.search)`);
  assert.deepEqual(themeModuleLoads, ['/static/js/theme.js'], 'theme runtime loaded under multiple module URLs');
  const startupCanvases = await evaluate(`[...document.querySelectorAll('[data-background-effect-canvas]')].map(canvas => canvas.id)`);
  assert.deepEqual(startupCanvases, ['clanker-routefield-canvas'], 'cold start mounted more than one background scene');
  const vanillaPresentation = await evaluate(`(async () => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    const proto=CanvasRenderingContext2D.prototype;
    const clearRect=proto.clearRect;
    const drawImage=proto.drawImage;
    const fillRect=proto.fillRect;
    const fill=proto.fill;
    const stroke=proto.stroke;
    let visibleClears=0;
    let copiedFrames=0;
    const compositeModes=new Set();
    const recordMode=context => {
      if (context.canvas===canvas) compositeModes.add(context.globalCompositeOperation);
    };
    proto.clearRect=function(...args) {
      if (this.canvas===canvas) {
        visibleClears+=1;
        recordMode(this);
      }
      return clearRect.apply(this,args);
    };
    proto.drawImage=function(...args) {
      if (this.canvas===canvas) {
        copiedFrames+=1;
        recordMode(this);
      }
      return drawImage.apply(this,args);
    };
    proto.fillRect=function(...args) {
      recordMode(this);
      return fillRect.apply(this,args);
    };
    proto.fill=function(...args) {
      recordMode(this);
      return fill.apply(this,args);
    };
    proto.stroke=function(...args) {
      recordMode(this);
      return stroke.apply(this,args);
    };
    try { await new Promise(resolve=>setTimeout(resolve,260)); }
    finally {
      proto.clearRect=clearRect;
      proto.drawImage=drawImage;
      proto.fillRect=fillRect;
      proto.fill=fill;
      proto.stroke=stroke;
    }
    return {
      visibleClears,
      copiedFrames,
      compositeModes:[...compositeModes],
    };
  })()`);
  assert(vanillaPresentation.visibleClears >= 10, `only ${vanillaPresentation.visibleClears} vanilla-style frames were painted`);
  assert.equal(vanillaPresentation.copiedFrames, 0, 'custom animation copied a second full-size canvas each frame');
  assert.deepEqual(vanillaPresentation.compositeModes, ['source-over'], 'custom animation used a non-vanilla compositing mode');
  const teardown = await evaluate(`(async () => {
    const select=document.getElementById('theme-bg-pattern-select');
    const oldCanvas=document.getElementById('clanker-routefield-canvas');
    select.value='clanker-radar';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(resolve=>setTimeout(resolve,80));
    const result={
      oldConnected:oldCanvas.isConnected,
      active:[...document.querySelectorAll('[data-background-effect-canvas]')].map(canvas=>canvas.id),
    };
    select.value='clanker-routefield';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    return result;
  })()`);
  assert.equal(teardown.oldConnected, false);
  assert.deepEqual(teardown.active, ['clanker-radar-canvas']);
  await waitFor("document.getElementById('clanker-routefield-canvas')?.isConnected", 'restored route field');

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
  assert(dark.liga && dark.fredoka);
  assert.match(dark.favicon, /M16 3 29 27H3Z/); assert.doesNotMatch(dark.favicon, /M16 4L16 22L6 22Z/);
  assert.match(dark.projectMark, /M8\.5 17Q16 7 23\.5 17/);
  assert.equal(dark.sidebarTexture, 'none'); assert.doesNotMatch(dark.sidebarTexture, /url\(/);
  assert.notEqual(dark.inputShadow, 'none'); assert.equal(dark.sendBorder, '2px');
  const paneCoverage = await evaluate(`(() => {
    const canvas = document.getElementById('clanker-routefield-canvas');
    const pane = document.getElementById('chat-container');
    const rect = element => {
      const value = element?.getBoundingClientRect();
      return value && { left:value.left, top:value.top, right:value.right, bottom:value.bottom, width:value.width, height:value.height };
    };
    return {
      parent: canvas?.parentElement?.id || null,
      position: canvas ? getComputedStyle(canvas).position : null,
      zIndex: canvas ? getComputedStyle(canvas).zIndex : null,
      overflow: pane ? getComputedStyle(pane).overflow : null,
      viewport: { width:innerWidth, height:innerHeight },
      canvas: rect(canvas),
      pane: rect(pane),
    };
  })()`);
  assert.equal(paneCoverage.parent, 'chat-container');
  assert.equal(paneCoverage.position, 'absolute');
  assert.equal(paneCoverage.zIndex, '-1');
  assert.equal(paneCoverage.overflow, 'hidden');
  assert(Math.abs(paneCoverage.canvas.left) <= 1 && Math.abs(paneCoverage.canvas.top) <= 1, 'canvas was not aligned to the viewport crop');
  assert(Math.abs(paneCoverage.canvas.width - paneCoverage.viewport.width) <= 1, 'canvas backing viewport width changed with pane layout');
  assert(Math.abs(paneCoverage.canvas.height - paneCoverage.viewport.height) <= 1, 'canvas backing viewport height changed with pane layout');
  const sidebarStability = await evaluate(`(async () => {
    const canvas = document.getElementById('clanker-routefield-canvas');
    const snapshot = () => {
      const rect = canvas.getBoundingClientRect();
      return {
        backing: { width:canvas.width, height:canvas.height },
        crop: { left:rect.left, top:rect.top, width:rect.width, height:rect.height },
      };
    };
    const pickToggle = () => [...document.querySelectorAll('#sidebar-toggle-btn, #hamburger-btn')]
      .find(button => getComputedStyle(button).display !== 'none');
    const clickToggle = () => { const button = pickToggle(); if (!button) return false; button.click(); return true; };
    const before = snapshot();
    clickToggle();
    await new Promise(resolve => setTimeout(resolve, 320));
    const collapsed = snapshot();
    clickToggle();
    await new Promise(resolve => setTimeout(resolve, 320));
    const restored = snapshot();
    return { before, collapsed, restored, viewport:{ width:innerWidth, height:innerHeight } };
  })()`);
  assert.deepEqual(sidebarStability.collapsed.backing, sidebarStability.before.backing, 'sidebar toggle resized the animation canvas');
  assert.deepEqual(sidebarStability.restored.backing, sidebarStability.before.backing, 'sidebar restore resized the animation canvas');
  for (const state of [sidebarStability.before, sidebarStability.collapsed, sidebarStability.restored]) {
    assert(Math.abs(state.crop.left) <= 1 && Math.abs(state.crop.top) <= 1, 'sidebar toggle moved the animation crop');
    assert(Math.abs(state.crop.width - sidebarStability.viewport.width) <= 1, 'sidebar toggle changed the animation crop width');
    assert(Math.abs(state.crop.height - sidebarStability.viewport.height) <= 1, 'sidebar toggle changed the animation crop height');
  }
  const darkFrameA = await canvasState('clanker-routefield-canvas');
  await new Promise(resolve => setTimeout(resolve, 260));
  const darkFrameB = await canvasState('clanker-routefield-canvas');
  assert(darkFrameA?.painted > 0); assert.notEqual(darkFrameA.hash, darkFrameB?.hash);
  assert(darkFrameA.painted >= Math.floor((darkFrameA.width * darkFrameA.height / 16) * 0.05), `route field only painted ${darkFrameA.painted} sampled pixels`);
  const routeStability = await canvasChange('clanker-routefield-canvas', 320);
  assert(routeStability?.changed > 0); assert(routeStability.ratio < 0.08, `route field changed ${Math.round(routeStability.ratio * 100)}% of painted samples`);
  const routeCadence = await canvasCadence('clanker-routefield-canvas');
  assert(routeCadence?.paints >= 10, `route field only painted ${routeCadence?.paints || 0} frames`);
  assert(routeCadence.min >= 7, `route field rendered twice inside a single frame (${routeCadence.min.toFixed(1)}ms)`);
  assert(routeCadence.median < 24, `route field median frame interval was ${routeCadence.median.toFixed(1)}ms`);
  await screenshot('clanker-dark-page');
  await screenshot('clanker-dark', 'popup');

  const patternResults = {};
  for (const [pattern, canvasId, screenshotName, minimumPaintedRatio] of [
    ['clanker-kene-weave', 'clanker-kene-weave-canvas', 'clanker-kene-weave', 0.16],
    ['clanker-radar', 'clanker-radar-canvas', 'clanker-radar', 0.28],
    ['clanker-gem-drift', 'clanker-gem-drift-canvas', 'clanker-gem-drift', 0.025],
    ['clanker-emoji-drift', 'clanker-emoji-drift-canvas', 'clanker-emoji-drift', 0.012],
    ['clanker-matrix-rain', 'clanker-matrix-rain-canvas', 'clanker-matrix-rain', 0.018],
    ['clanker-emoji-rain', 'clanker-emoji-rain-canvas', 'clanker-emoji-rain', 0.014],
  ]) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change', {bubbles:true})); return select.value; })()`);
    await waitFor(`document.body.classList.contains('bg-pattern-${pattern}') && document.getElementById('${canvasId}')?.isConnected`, pattern);
    const frameA = await canvasState(canvasId);
    await new Promise(resolve => setTimeout(resolve, 320));
    const frameB = await canvasState(canvasId);
    assert(frameA?.painted > 0, `${pattern} did not paint: ${JSON.stringify(frameA)}`);
    assert(frameA.painted >= Math.floor((frameA.width * frameA.height / 16) * minimumPaintedRatio), `${pattern} only painted ${frameA.painted} sampled pixels`);
    assert.notEqual(frameA.hash, frameB?.hash, `${pattern} did not animate`);
    patternResults[pattern] = { frameA, frameB };
    await screenshot(screenshotName);
  }

  const effectControlResults = await evaluate(`(async () => {
    const select=document.getElementById('theme-bg-pattern-select');
    const pause=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const choose=async pattern=>{
      select.value=pattern;
      select.dispatchEvent(new Event('change',{bubbles:true}));
      await pause();
    };
    const control=(pattern,key)=>document.getElementById('theme-bg-effect-'+pattern+'-'+key);
    const setRange=(input,value)=>{
      input.value=String(value);
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.dispatchEvent(new Event('change',{bubbles:true}));
    };
    const setToggle=(input,value)=>{
      input.checked=!!value;
      input.dispatchEvent(new Event('change',{bubbles:true}));
    };
    const enableAdvanced=pattern=>{
      const toggle=control(pattern,'advancedSettings');
      const wasOff=toggle && !toggle.checked;
      setToggle(toggle,true);
      return wasOff;
    };

    await choose('clanker-kene-weave');
    const keneKeys=['snakeCount','snakeLengthVariation','snakeLifetimeVariation','shorterLastLonger','longerDisappearSooner'];
    const keneAdvancedOff=enableAdvanced('clanker-kene-weave');
    const shortScaleAbsent=!control('clanker-kene-weave','shorterLifetimeScale');
    const longScaleAbsent=!control('clanker-kene-weave','longerLifetimeScale');
    setRange(control('clanker-kene-weave','snakeCount'), 11);
    setRange(control('clanker-kene-weave','snakeLengthVariation'), 60);
    setRange(control('clanker-kene-weave','snakeLifetimeVariation'), 45);
    const shorter=control('clanker-kene-weave','shorterLastLonger');
    shorter.checked=true;
    shorter.dispatchEvent(new Event('change',{bubbles:true}));
    setRange(control('clanker-kene-weave','shorterLifetimeScale'), 150);
    const longer=control('clanker-kene-weave','longerDisappearSooner');
    longer.checked=true;
    longer.dispatchEvent(new Event('change',{bubbles:true}));
    setRange(control('clanker-kene-weave','longerLifetimeScale'), 55);
    await pause();
    const kene={
      controls:keneKeys.every(key=>!!control('clanker-kene-weave',key)),
      shortScaleAbsent,
      longScaleAbsent,
      shortScale:!!control('clanker-kene-weave','shorterLifetimeScale'),
      longScale:!!control('clanker-kene-weave','longerLifetimeScale'),
      advancedOff:keneAdvancedOff,
      stable:document.getElementById('clanker-kene-weave-canvas')?.isConnected,
    };

    await choose('clanker-gem-drift');
    const gemAdvancedOff=enableAdvanced('clanker-gem-drift');
    const gemSize=control('clanker-gem-drift','gemSizeVariation');
    const gemSizeRange={min:gemSize.min,max:gemSize.max,value:gemSize.value};
    setRange(control('clanker-gem-drift','driftSpeed'), 140);
    setRange(control('clanker-gem-drift','driftSpeedVariation'), 70);
    setRange(gemSize, 0);
    setRange(gemSize, 999);
    setRange(gemSize, 650);
    setRange(control('clanker-gem-drift','intensityVariation'), 425);
    setRange(control('clanker-gem-drift','middleIntensity'), 120);
    setRange(control('clanker-gem-drift','totalQuantity'), 160);
    setRange(control('clanker-gem-drift','glowLikelihood'), 35);
    setRange(control('clanker-gem-drift','rotationLikelihood'), 65);
    setRange(control('clanker-gem-drift','rotationSpeed'), 130);
    setRange(control('clanker-gem-drift','rotationSpeedVariation'), 45);
    await pause();
    const gem={
      controls:['driftSpeed','driftSpeedVariation','gemSizeVariation','intensityVariation','middleIntensity','totalQuantity','glowLikelihood','rotationLikelihood','rotationSpeed','rotationSpeedVariation'].every(key=>!!control('clanker-gem-drift',key)),
      advancedOff:gemAdvancedOff,
      stable:document.getElementById('clanker-gem-drift-canvas')?.isConnected,
      sizeRange:gemSizeRange,
    };

    await choose('clanker-emoji-drift');
    const emojiAdvancedOff=enableAdvanced('clanker-emoji-drift');
    const emojiCanvas=document.getElementById('clanker-emoji-drift-canvas');
    const emojiSize=control('clanker-emoji-drift','gemSizeVariation');
    const emojiSizeRange={min:emojiSize.min,max:emojiSize.max,value:emojiSize.value};
    setRange(control('clanker-emoji-drift','driftSpeed'), 80);
    setRange(control('clanker-emoji-drift','driftSpeedVariation'), 55);
    setRange(control('clanker-emoji-drift','gemSizeVariation'), 825);
    setRange(control('clanker-emoji-drift','intensityVariation'), 480);
    setRange(control('clanker-emoji-drift','middleIntensity'), 80);
    setRange(control('clanker-emoji-drift','totalQuantity'), 120);
    setRange(control('clanker-emoji-drift','glowLikelihood'), 45);
    setRange(control('clanker-emoji-drift','rotationLikelihood'), 75);
    setRange(control('clanker-emoji-drift','rotationSpeed'), 90);
    setRange(control('clanker-emoji-drift','rotationSpeedVariation'), 60);
    let emojiDraws=0;
    const proto=CanvasRenderingContext2D.prototype;
    const drawImage=proto.drawImage;
    proto.drawImage=function(...args) {
      if (this.canvas===emojiCanvas) emojiDraws++;
      return drawImage.call(this,...args);
    };
    await pause();
    proto.drawImage=drawImage;
    const emoji={
      controls:['driftSpeed','driftSpeedVariation','gemSizeVariation','intensityVariation','middleIntensity','totalQuantity','glowLikelihood','rotationLikelihood','rotationSpeed','rotationSpeedVariation'].every(key=>!!control('clanker-emoji-drift',key)),
      advancedOff:emojiAdvancedOff,
      stable:document.getElementById('clanker-emoji-drift-canvas')?.isConnected,
      font:document.fonts.check('24px "Noto Color Emoji"'),
      draws:emojiDraws,
      sizeRange:emojiSizeRange,
    };

    await choose('clanker-matrix-rain');
    const matrixAdvancedOff=enableAdvanced('clanker-matrix-rain');
    const rainToggleKeys=new Set(['splashRainDown','splashRainWaves','splashColorVarianceEnabled','splashRareUpward']);
    const rainKeys=['splashQuantity','splashRainSpeed','splashRainFlicker','splashRainSpread','splashRainDown','splashRainReverseChance','splashRainWaves','splashCharVariety','splashMinOpacity','splashMaxOpacity','splashSizeVariance','splashColorVarianceEnabled','splashColorVariance','splashRareUpward','splashBounce','splashGravity','splashCollisionForce'];
    // Rare upward toggle defaults ON (S24). Capture scene identity across the
    // advanced-settings toggle — UI-only controls must not reset droplets.
    const rareToggle=control('clanker-matrix-rain','splashRareUpward');
    const rareDefaultOn=!!(rareToggle && rareToggle.checked);
    const rainSceneBefore=document.getElementById('clanker-matrix-rain-canvas')?.__backgroundScene;
    setToggle(control('clanker-matrix-rain','advancedSettings'), false);
    await pause();
    setToggle(control('clanker-matrix-rain','advancedSettings'), true);
    await pause();
    const rainSceneAfterAdvanced=document.getElementById('clanker-matrix-rain-canvas')?.__backgroundScene;
    const rainAdvancedStable=!!rainSceneBefore && rainSceneBefore===rainSceneAfterAdvanced;
    rainKeys.filter(key=>!rainToggleKeys.has(key)).forEach((key,index)=>setRange(control('clanker-matrix-rain',key), [2.2,1.8,.45,2.3,12.5,.5,.1,.9,.75,.7,2,1.2,3.1][index]));
    setToggle(control('clanker-matrix-rain','splashRainDown'), false);
    setToggle(control('clanker-matrix-rain','splashRainWaves'), true);
    setToggle(control('clanker-matrix-rain','splashColorVarianceEnabled'), true);
    setToggle(control('clanker-matrix-rain','splashRareUpward'), true);
    setToggle(control('clanker-matrix-rain','splashEmojiMix'), true);
    setRange(control('clanker-matrix-rain','splashEmojiRarity'), 1000);
    await pause();
    const matrixRain={
      controls:rainKeys.every(key=>!!control('clanker-matrix-rain',key)),
      emojiControls:!!control('clanker-matrix-rain','splashEmojiMix') && !!control('clanker-matrix-rain','splashEmojiRarity'),
      advancedOff:matrixAdvancedOff,
      rareDefaultOn,
      rainAdvancedStable,
      owner:!!window.__openClankBackgroundOwner,
      sceneStreams:document.getElementById('clanker-matrix-rain-canvas')?.__backgroundScene?.streams?.length||0,
      stable:document.getElementById('clanker-matrix-rain-canvas')?.isConnected,
    };

    await choose('clanker-emoji-rain');
    const emojiRainAdvancedOff=enableAdvanced('clanker-emoji-rain');
    const emojiRainValues={splashQuantity:1.6,splashRainSpeed:1.4,splashRainFlicker:.3,splashRainSpread:1.7,splashRainDown:true,splashRainReverseChance:78,splashRainWaves:true,splashCharVariety:.55,splashMinOpacity:.5,splashMaxOpacity:.9,splashSizeVariance:.8,splashColorVarianceEnabled:true,splashColorVariance:.8,splashBounce:2.4,splashGravity:.8,splashCollisionForce:2.4};
    rainKeys.forEach(key=>{
      const value=emojiRainValues[key];
      if (rainToggleKeys.has(key)) setToggle(control('clanker-emoji-rain',key), key==='splashRareUpward' ? true : value > .5);
      else setRange(control('clanker-emoji-rain',key), value);
    });
    await pause();
    const emojiRain={
      controls:rainKeys.every(key=>!!control('clanker-emoji-rain',key)),
      advancedOff:emojiRainAdvancedOff,
      rareDefaultOn:!!control('clanker-emoji-rain','splashRareUpward')?.checked !== false,
      stable:document.getElementById('clanker-emoji-rain-canvas')?.isConnected,
    };

    await choose('clanker-radar');
    const saved=JSON.parse(localStorage.getItem('odysseus-theme'));
    return { kene, gem, emoji, matrixRain, emojiRain, hidden:document.getElementById('theme-bg-effect-controls')?.hidden, saved, controls:saved?.bgEffectControls };
  })()`);
  assert(effectControlResults.kene.controls);
  assert(effectControlResults.kene.advancedOff, 'Signal Weave advanced settings defaulted on');
  assert(effectControlResults.kene.shortScaleAbsent && effectControlResults.kene.longScaleAbsent);
  assert(effectControlResults.kene.shortScale && effectControlResults.kene.longScale);
  assert(effectControlResults.kene.stable, 'Signal Weave controls remounted its canvas');
  assert(effectControlResults.gem.controls && effectControlResults.gem.stable, 'Gem Drift controls remounted its canvas');
  assert(effectControlResults.gem.advancedOff, 'Gem Drift advanced settings defaulted on');
  assert.deepEqual(effectControlResults.gem.sizeRange, { min:'0', max:'999', value:'100' });
  assert(effectControlResults.emoji.controls && effectControlResults.emoji.stable, 'Emoji Drift controls remounted its canvas');
  assert(effectControlResults.emoji.advancedOff, 'Emoji Drift advanced settings defaulted on');
  assert.deepEqual(effectControlResults.emoji.sizeRange, { min:'0', max:'999', value:'100' });
  assert(effectControlResults.emoji.font, 'Emoji Drift did not resolve Noto Color Emoji');
  assert(effectControlResults.emoji.draws > 8, 'Emoji Drift did not draw cached emoji sprites');
  assert(effectControlResults.matrixRain.controls && effectControlResults.matrixRain.emojiControls && effectControlResults.matrixRain.stable, 'Matrix Rain controls remounted or went missing');
  assert(effectControlResults.matrixRain.advancedOff, 'Matrix Rain advanced settings defaulted on');
  assert(effectControlResults.matrixRain.rareDefaultOn, 'Rare upward drop toggle must default ON');
  assert(effectControlResults.matrixRain.rainAdvancedStable, 'Toggling Advanced settings reset droplets / replaced the rain scene');
  assert(effectControlResults.matrixRain.owner, 'Matrix Rain lost its single background owner');
  assert(effectControlResults.matrixRain.sceneStreams > 0, 'Matrix Rain scene exposed no streams');
  assert(effectControlResults.emojiRain.controls && effectControlResults.emojiRain.stable, 'Emoji Rain controls remounted or went missing');
  assert(effectControlResults.emojiRain.advancedOff, 'Emoji Rain advanced settings defaulted on');
  assert(effectControlResults.hidden, 'effects without controls left a stale control panel visible');
  assert(effectControlResults.controls, `effect controls did not persist: ${JSON.stringify(effectControlResults.saved)}`);
  assert.equal(effectControlResults.controls['clanker-kene-weave'].snakeCount, 11);
  assert.equal(effectControlResults.controls['clanker-kene-weave'].shorterLifetimeScale, 150);
  assert.equal(effectControlResults.controls['clanker-kene-weave'].longerLifetimeScale, 55);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].driftSpeed, 140);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].driftSpeedVariation, 70);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].gemSizeVariation, 650);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].intensityVariation, 425);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].middleIntensity, 120);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].totalQuantity, 160);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].glowLikelihood, 35);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].rotationLikelihood, 65);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].rotationSpeed, 130);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].rotationSpeedVariation, 45);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].driftSpeed, 80);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].driftSpeedVariation, 55);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].gemSizeVariation, 825);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].intensityVariation, 480);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].middleIntensity, 80);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].totalQuantity, 120);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].glowLikelihood, 45);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].rotationLikelihood, 75);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].rotationSpeed, 90);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].rotationSpeedVariation, 60);
  assert.equal(effectControlResults.controls['clanker-matrix-rain'].splashQuantity, 2.2);
  assert.equal(effectControlResults.controls['clanker-matrix-rain'].splashRainSpeed, 1.8);
  assert.equal(effectControlResults.controls['clanker-matrix-rain'].splashRainReverseChance, 12.5);
  assert.equal(effectControlResults.controls['clanker-matrix-rain'].splashEmojiRarity, 1000);
  assert.equal(effectControlResults.controls['clanker-emoji-rain'].splashQuantity, 1.6);
  assert.equal(effectControlResults.controls['clanker-emoji-rain'].splashRainFlicker, .3);
  const extremeResults = {};
  for (const [pattern, canvasId] of [
    ['clanker-gem-drift', 'clanker-gem-drift-canvas'],
    ['clanker-emoji-drift', 'clanker-emoji-drift-canvas'],
  ]) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.isConnected`, `${pattern} extreme controls`);
    extremeResults[pattern] = await canvasState(canvasId);
  }
  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-kene-weave'; select.dispatchEvent(new Event('change',{bubbles:true})); })()");
  await waitFor("document.getElementById('clanker-kene-weave-canvas')?.isConnected", 'Signal Weave control screenshot');
  await evaluate("(() => { document.getElementById('theme-modal')?.classList.remove('hidden'); document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]')?.click(); document.getElementById('theme-bg-effect-controls')?.scrollIntoView({block:'center'}); })()");
  await screenshot('clanker-kene-controls', 'popup');
  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-gem-drift'; select.dispatchEvent(new Event('change',{bubbles:true})); document.getElementById('theme-bg-effect-controls')?.scrollIntoView({block:'center'}); })()");
  await waitFor("document.getElementById('clanker-gem-drift-canvas')?.isConnected", 'Gem Drift control screenshot');
  await screenshot('clanker-gem-controls', 'popup');
  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-emoji-drift'; select.dispatchEvent(new Event('change',{bubbles:true})); document.getElementById('theme-bg-effect-controls')?.scrollIntoView({block:'center'}); })()");
  await waitFor("document.getElementById('clanker-emoji-drift-canvas')?.isConnected", 'Emoji Drift control screenshot');
  await screenshot('clanker-emoji-controls', 'popup');
  await evaluate(`(() => {
    const select=document.getElementById('theme-bg-pattern-select');
    const control=(pattern,key)=>document.getElementById('theme-bg-effect-'+pattern+'-'+key);
    const setRange=(input,value)=>{
      input.value=String(value);
      input.dispatchEvent(new Event('input',{bubbles:true}));
      input.dispatchEvent(new Event('change',{bubbles:true}));
    };
    select.value='clanker-kene-weave';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    setRange(control('clanker-kene-weave','snakeCount'), 7);
    setRange(control('clanker-kene-weave','snakeLengthVariation'), 0);
    setRange(control('clanker-kene-weave','snakeLifetimeVariation'), 0);
    let toggle=control('clanker-kene-weave','shorterLastLonger');
    if (toggle.checked) { toggle.checked=false; toggle.dispatchEvent(new Event('change',{bubbles:true})); }
    toggle=control('clanker-kene-weave','longerDisappearSooner');
    if (toggle.checked) { toggle.checked=false; toggle.dispatchEvent(new Event('change',{bubbles:true})); }
    select.value='clanker-gem-drift';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    setRange(control('clanker-gem-drift','driftSpeed'), 100);
    setRange(control('clanker-gem-drift','driftSpeedVariation'), 20);
    setRange(control('clanker-gem-drift','gemSizeVariation'), 100);
    setRange(control('clanker-gem-drift','intensityVariation'), 100);
    setRange(control('clanker-gem-drift','middleIntensity'), 100);
    setRange(control('clanker-gem-drift','totalQuantity'), 100);
    setRange(control('clanker-gem-drift','glowLikelihood'), 11);
    setRange(control('clanker-gem-drift','rotationLikelihood'), 35);
    setRange(control('clanker-gem-drift','rotationSpeed'), 100);
    setRange(control('clanker-gem-drift','rotationSpeedVariation'), 30);
    select.value='clanker-emoji-drift';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    setRange(control('clanker-emoji-drift','driftSpeed'), 100);
    setRange(control('clanker-emoji-drift','driftSpeedVariation'), 20);
    setRange(control('clanker-emoji-drift','gemSizeVariation'), 100);
    setRange(control('clanker-emoji-drift','intensityVariation'), 100);
    setRange(control('clanker-emoji-drift','middleIntensity'), 100);
    setRange(control('clanker-emoji-drift','totalQuantity'), 100);
    setRange(control('clanker-emoji-drift','glowLikelihood'), 11);
    setRange(control('clanker-emoji-drift','rotationLikelihood'), 35);
    setRange(control('clanker-emoji-drift','rotationSpeed'), 100);
    setRange(control('clanker-emoji-drift','rotationSpeedVariation'), 30);
    select.value='clanker-kene-weave';
    select.dispatchEvent(new Event('change',{bubbles:true}));
  })()`);
  await waitFor("document.getElementById('clanker-kene-weave-canvas')?.isConnected", 'restored Signal Weave defaults');

  const canvasPatternIds = {
    'clanker-routefield':'clanker-routefield-canvas',
    'clanker-kene-weave':'clanker-kene-weave-canvas',
    'clanker-radar':'clanker-radar-canvas',
    'clanker-gem-drift':'clanker-gem-drift-canvas',
    'clanker-emoji-drift':'clanker-emoji-drift-canvas',
    'clanker-matrix-rain':'clanker-matrix-rain-canvas',
    'clanker-emoji-rain':'clanker-emoji-rain-canvas',
    synapse:'synapse-canvas', rain:'rain-canvas', constellations:'constellations-canvas',
    'perlin-flow':'perlin-flow-canvas', petals:'petals-canvas', sparkles:'sparkles-canvas', embers:'embers-canvas',
  };
  const patternOrder = [
    'none', 'clanker-routefield', 'clanker-kene-weave', 'clanker-radar',
    'clanker-gem-drift', 'clanker-emoji-drift', 'clanker-matrix-rain', 'clanker-emoji-rain', 'clanker-blueprint', 'dots', 'synapse', 'rain',
    'constellations', 'perlin-flow', 'petals', 'sparkles', 'embers',
  ];
  assert.deepEqual(await evaluate("[...document.getElementById('theme-bg-pattern-select').options].map(option => option.value)"), patternOrder);
  const transitionMatrix = { checked: 0, failures: [] };
  for (const from of patternOrder) {
    const row = await evaluate(`(async () => {
      const from=${JSON.stringify(from)};
      const patterns=${JSON.stringify(patternOrder)};
      const canvasIds=${JSON.stringify(canvasPatternIds)};
      const select=document.getElementById('theme-bg-pattern-select');
      const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
      const failures=[];
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
          && (!expectedCanvas || (canvases[0].id===expectedCanvas && canvases[0].isConnected
            && background.backgroundImage==='none' && background.animationName==='none'));
        if (!valid) failures.push({from,to,classes,canvases:canvases.map(node=>({id:node.id,connected:node.isConnected})),backgroundImage:background.backgroundImage,animationName:background.animationName});
      }
      return {checked:patterns.length,failures};
    })()`);
    transitionMatrix.checked += row.checked;
    transitionMatrix.failures.push(...row.failures);
  }
  assert.equal(transitionMatrix.checked, patternOrder.length ** 2);
  assert.deepEqual(transitionMatrix.failures, []);

  const effectMotionResults = {};
  for (const [pattern, canvasId] of Object.entries(canvasPatternIds)) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.isConnected`, `${pattern} canvas owner`);
    await new Promise(resolve => setTimeout(resolve, 420));
    const frameA = await canvasState(canvasId);
    await new Promise(resolve => setTimeout(resolve, 360));
    const frameB = await canvasState(canvasId);
    const change = await canvasChange(canvasId, 360);
    assert(frameA?.painted > 0, `${pattern} did not paint`);
    assert.notEqual(frameA.hash, frameB?.hash, `${pattern} did not animate`);
    const coverageLimit = pattern.endsWith('-rain') ? 0.18 : 0.08;
    assert(change?.coverage < coverageLimit, `${pattern} changed ${Math.round((change?.coverage || 0) * 100)}% of sampled pixels`);
    const cadence = await canvasCadenceStable(canvasId);
    effectMotionResults[pattern] = { frameA, frameB, change, cadence };
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

  await evaluate(`localStorage.removeItem('odysseus-theme:scope:theme-test'); localStorage.setItem('odysseus-theme', JSON.stringify({
    name:'clanker-dark',
    colors:{bg:'#191A1E',fg:'#FFF4D6',panel:'#25272C',border:'#555A62',red:'#5A9EF5'}
  }))`);
  await reloadAndWait("document.querySelector('#themeGrid .theme-swatch.active')?.dataset.theme === 'clanker-dark' && document.getElementById('clanker-routefield-canvas')", 'legacy Clanker migration');
  const migration = await evaluate(`(() => { const saved=JSON.parse(localStorage.getItem('odysseus-theme')); return {saved,bg:getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(),classes:[...document.body.classList]}; })()`);
  assert.equal(migration.bg.toUpperCase(), '#191A1E');
  assert.equal(migration.saved.colors.bg.toUpperCase(), '#191A1E');
  assert.equal(migration.saved.bgPattern, 'clanker-routefield');
  assert.equal(migration.saved.bgEffectColor.toUpperCase(), '#62C7E8');
  assert.equal(migration.saved.bgEffectIntensity, 0.64);

  await command('Emulation.setEmulatedMedia', { features:[{ name:'prefers-reduced-motion', value:'reduce' }] });
  // CDP updates matchMedia synchronously but delivers its `change` callback
  // on a later task. Let the already-mounted owner repaint at time zero before
  // sampling; otherwise frameA can capture the final animated bitmap and make
  // a deterministic reduced-motion canvas look like it moved.
  await new Promise(resolve => setTimeout(resolve, 120));
  await waitFor("!!document.querySelector('#themeGrid [data-theme=\"clanker-dark\"]')", 'Clanker Dark swatch');
  assert.equal(await evaluate("(() => { const sw=document.querySelector('#themeGrid [data-theme=\"clanker-dark\"]'); if (!sw) return false; sw.click(); return true; })()"), true);
  const reducedResults = {};
  for (const [pattern, canvasId] of Object.entries(canvasPatternIds)) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.isConnected`, `${pattern} reduced motion`);
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
  await waitFor("document.getElementById('clanker-routefield-canvas')?.isConnected", 'restored reduced route field');
  await command('Emulation.setEmulatedMedia', { features:[] });

  await command('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:1, mobile:true });
  await reloadAndWait("document.readyState === 'complete' && innerWidth === 390 && document.getElementById('clanker-routefield-canvas')?.isConnected", 'mobile route field');
  const mobile = await evaluate(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    const pane=document.getElementById('chat-container').getBoundingClientRect();
    return { innerWidth, innerHeight, scrollWidth:document.documentElement.scrollWidth, canvasWidth:canvas?.width, canvasHeight:canvas?.height, paneWidth:pane.width, paneHeight:pane.height, classes:[...document.body.classList] };
  })()`);
  assert.equal(mobile.scrollWidth, mobile.innerWidth);
  assert.equal(mobile.canvasWidth, mobile.innerWidth);
  assert.equal(mobile.canvasHeight, mobile.innerHeight);
  assert(mobile.classes.includes('bg-pattern-clanker-routefield'));
  await screenshot('clanker-dark-mobile');
  const mobilePatternResults = {};
  for (const [pattern, canvasId] of Object.entries(canvasPatternIds).filter(([name]) => name.startsWith('clanker-'))) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.isConnected`, `${pattern} mobile canvas`);
    const frame = await canvasState(canvasId);
    assert.equal(frame?.width, mobile.innerWidth, `${pattern} mobile width`);
    assert.equal(frame?.height, mobile.innerHeight, `${pattern} mobile height`);
    assert(frame.painted > 0, `${pattern} mobile canvas was blank`);
    mobilePatternResults[pattern] = { frame };
    await screenshot(`${pattern}-mobile`);
  }

  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-routefield'; select.dispatchEvent(new Event('change',{bubbles:true})); })()");
  await waitFor("document.getElementById('clanker-routefield-canvas')?.isConnected", 'route field before live DPR resize');
  const beforeDprResize = await evaluate(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    globalThis.__clankerResizeProbe={ canvas };
    return { width:canvas?.width, height:canvas?.height };
  })()`);
  await command('Emulation.setDeviceMetricsOverride', { width:412, height:915, deviceScaleFactor:2, mobile:true });
  await waitFor(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    return innerWidth === 412 && canvas?.width === Math.round(innerWidth * devicePixelRatio) && canvas?.height === Math.round(innerHeight * devicePixelRatio);
  })()`, 'live DPR resize');
  const dprResize = await evaluate(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    return {
      sameCanvas:canvas===globalThis.__clankerResizeProbe?.canvas,
      width:canvas?.width,
      height:canvas?.height,
    };
  })()`);
  const dprResizeIdentity = await evaluate(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    return {
      canvasId:canvas?.id,
      width:canvas?.width,
      height:canvas?.height,
    };
  })()`);
  assert.equal(dprResizeIdentity.canvasId, 'clanker-routefield-canvas');
  assert.equal(dprResize.sameCanvas, true, 'live viewport change remounted the running canvas');
  assert.notEqual(dprResize.width, beforeDprResize.width, 'live viewport change did not resize the running canvas');
  assert.notEqual(dprResize.height, beforeDprResize.height, 'live viewport change did not resize the running canvas');
  const dprViewport = await evaluate("({ width:Math.round(innerWidth * devicePixelRatio), height:Math.round(innerHeight * devicePixelRatio) })");
  assert.equal(dprResizeIdentity.width, dprViewport.width);
  assert.equal(dprResizeIdentity.height, dprViewport.height);

  await command('Emulation.setDeviceMetricsOverride', { width:1440, height:1000, deviceScaleFactor:1, mobile:false });
  await evaluate("document.querySelector('#themeGrid [data-theme=\"clanker-light\"]').click()");
  await waitFor("JSON.parse(localStorage.getItem('odysseus-theme'))?.name === 'clanker-light'", 'saved light theme before login');
  await command('Page.navigate', { url:`${base}/login` });
  await waitFor("document.readyState === 'complete' && document.body.classList.contains('theme-clanker-dark') && document.getElementById('clanker-routefield-canvas')?.isConnected", 'Clanker login theme');
  const login = await evaluate(`(async () => { await document.fonts.load("16px 'Liga Comic Mono'"); await document.fonts.load("32px 'Fredoka'"); const root=getComputedStyle(document.documentElement), body=getComputedStyle(document.body), card=getComputedStyle(document.querySelector('.card')); return { bg:root.getPropertyValue('--bg').trim(), savedName:JSON.parse(localStorage.getItem('odysseus-theme'))?.name, classes:[...document.body.classList], font:body.fontFamily, backgroundImage:body.backgroundImage, effectCanvasCount:document.querySelectorAll('[data-background-effect-canvas]').length, logoFont:getComputedStyle(document.querySelector('.logo span')).fontFamily, logoMark:document.querySelector('.logo-mark')?.innerHTML, favicon:decodeURIComponent(document.querySelector("link[rel='icon']").href.split(',')[1]), submitBackground:getComputedStyle(document.querySelector('#submitBtn')).backgroundColor, cardBorder:card.borderTopWidth, cardRadius:card.borderTopLeftRadius, cardShadow:card.boxShadow, liga:document.fonts.check("16px 'Liga Comic Mono'"), fredoka:document.fonts.check("32px 'Fredoka'") }; })()`);
  assert.match(login.font, /Liga Comic Mono/); assert.match(login.logoFont, /Fredoka/);
  assert.equal(login.bg.toUpperCase(), '#191A1E'); assert.equal(login.savedName, 'clanker-light');
  assert(login.classes.includes('theme-clanker-dark') && login.classes.includes('bg-pattern-clanker-routefield'));
  assert.equal(login.backgroundImage, 'none'); assert.equal(login.effectCanvasCount, 1);
  assert.match(login.favicon, /#F6BE48/i, 'login favicon uses the gold default accent');
  assert.equal(login.submitBackground, 'rgb(246, 190, 72)', 'login submit fallback uses the gold default accent');
  assert.equal(login.cardBorder, '2px'); assert.equal(login.cardRadius, '16px');
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
  await reloadAndWait("document.readyState === 'complete' && innerWidth === 390 && document.body.classList.contains('theme-clanker-dark') && document.getElementById('clanker-routefield-canvas')?.isConnected", 'mobile dark login');
  const mobileLogin = await evaluate(`(() => { const rect=document.querySelector('.card').getBoundingClientRect(); return {overflow:document.documentElement.scrollWidth-window.innerWidth,left:rect.left,right:rect.right,viewport:window.innerWidth}; })()`);
  assert(mobileLogin.overflow <= 0); assert(mobileLogin.left >= 0); assert(mobileLogin.right <= mobileLogin.viewport);
  await screenshot('clanker-login-mobile');
  assert.deepEqual(exceptions, []);
  process.stdout.write(`${JSON.stringify({ dark, paneCoverage, sidebarStability, routeStability, routeCadence, vanillaPresentation, patternResults, effectControlResults, extremeResults, transitionMatrix, effectMotionResults, light, original, fontViews, migration, reducedResults, mobile, mobilePatternResults, dprResize, dprResizeIdentity, login, mobileLogin, screenshots:outputDir }, null, 2)}\n`);
} finally {
  if (socket) socket.close();
  // Chromium can keep a renderer child alive after the parent receives
  // SIGTERM.  Do not leave a completed acceptance run hanging forever: give
  // the normal shutdown a bounded grace period, then force only this
  // disposable browser process down.
  const exited = new Promise(resolve => chromium.once('exit', resolve));
  chromium.kill('SIGTERM');
  await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 3000))]);
  if (chromium.exitCode === null) chromium.kill('SIGKILL');
  fs.rmSync(profile, { recursive:true, force:true });
}
