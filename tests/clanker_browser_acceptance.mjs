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
  const canvasSafetyState = id => evaluate(`(() => {
    const canvas=document.getElementById(${JSON.stringify(id)});
    if (!canvas || !canvas.width || !canvas.height) return null;
    const ctx=canvas.getContext('2d');
    const data=ctx.getImageData(0,0,canvas.width,canvas.height).data;
    let paintedPerimeter=0, maximumPerimeterAlpha=0;
    const visit=(x,y)=>{
      const alpha=data[(y*canvas.width+x)*4+3];
      if (alpha) paintedPerimeter+=1;
      maximumPerimeterAlpha=Math.max(maximumPerimeterAlpha,alpha);
    };
    for (let x=0; x<canvas.width; x+=1) {
      visit(x,0);
      if (canvas.height>1) visit(x,canvas.height-1);
    }
    for (let y=1; y<canvas.height-1; y+=1) {
      visit(0,y);
      if (canvas.width>1) visit(canvas.width-1,y);
    }

    const rect=canvas.getBoundingClientRect();
    const inset=canvas.__backgroundSafeInset || 0;
    const scene=canvas.__backgroundScene || {};
    const inside=(point,pad=0)=>point
      && point.x-pad>=inset && point.x+pad<=innerWidth-inset
      && point.y-pad>=inset && point.y+pad<=innerHeight-inset;
    let geometryViolations=0;
    if (scene.nodes) geometryViolations+=scene.nodes.filter(point=>!inside(point)).length;
    if (scene.routes) geometryViolations+=scene.routes.filter(route=>!inside({x:route.cx,y:route.cy})).length;
    if (scene.paths) geometryViolations+=scene.paths.flatMap(path=>path.points).filter(point=>!inside(point)).length;
    if (scene.junctions) geometryViolations+=scene.junctions.filter(point=>!inside(point)).length;
    if (scene.snakePoints) geometryViolations+=scene.snakePoints.flat().filter(point=>!inside(point)).length;
    if (scene.shards) geometryViolations+=scene.shards.filter(point=>!inside(point)).length;
    if (scene.centers) geometryViolations+=scene.centers.filter(center=>!inside(center,center.radius)).length;
    return {
      paintedPerimeter,
      maximumPerimeterAlpha,
      geometryViolations,
      safeInset:inset,
      rect:{ x:rect.x, y:rect.y, width:rect.width, height:rect.height },
      viewport:{ width:innerWidth, height:innerHeight, dpr:devicePixelRatio },
      backing:{ width:canvas.width, height:canvas.height },
    };
  })()`);
  const assertCanvasSafe = async (id, label = id) => {
    const state=await canvasSafetyState(id);
    assert(state, `${label} canvas was missing`);
    assert.equal(state.paintedPerimeter, 0, `${label} painted ${state.paintedPerimeter} clipped perimeter pixels`);
    assert.equal(state.maximumPerimeterAlpha, 0, `${label} left alpha ${state.maximumPerimeterAlpha} on its bitmap edge`);
    assert.equal(state.geometryViolations, 0, `${label} authored ${state.geometryViolations} primitives outside its safe area`);
    assert(state.safeInset >= 12, `${label} had no safe drawing gutter`);
    assert(Math.abs(state.rect.x) < 1 && Math.abs(state.rect.y) < 1, `${label} canvas was offset from the viewport`);
    assert(Math.abs(state.rect.width-state.viewport.width) < 1 && Math.abs(state.rect.height-state.viewport.height) < 1,
      `${label} canvas CSS size did not match the viewport`);
    assert.equal(state.backing.width, Math.floor(state.viewport.width*Math.min(state.viewport.dpr,2)), `${label} backing width did not match DPR`);
    assert.equal(state.backing.height, Math.floor(state.viewport.height*Math.min(state.viewport.dpr,2)), `${label} backing height did not match DPR`);
    return state;
  };
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
    const minimumPaints = id.startsWith('clanker-') ? 10 : 2;
    assert(cadence?.paints >= minimumPaints, `${id} only painted ${cadence?.paints || 0} frames`);
    assert(cadence.min >= 7, `${id} rendered twice inside a single frame (${cadence.min.toFixed(1)}ms)`);
    return cadence;
  };
  const canvasSceneStable = id => evaluate(`(async () => {
    const canvas=document.getElementById(${JSON.stringify(id)});
    if (!canvas) return false;
    const scene=canvas.__backgroundScene;
    const resizeCount=canvas.__backgroundResizeCount;
    for (let index=0; index<6; index+=1) window.dispatchEvent(new Event('resize'));
    await new Promise(resolve=>setTimeout(resolve, 140));
    return canvas.__backgroundScene===scene && canvas.__backgroundResizeCount===resizeCount;
  })()`);
  const canvasPatternStable = id => evaluate(`(async () => {
    const canvas=document.getElementById(${JSON.stringify(id)});
    const select=document.getElementById('theme-bg-pattern-select');
    if (!canvas || !select) return false;
    const scene=canvas.__backgroundScene;
    select.dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    return document.getElementById(${JSON.stringify(id)})===canvas && canvas.__backgroundScene===scene;
  })()`);

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Network.enable');
  await command('Network.setCacheDisabled', { cacheDisabled:true });
  await command('Network.setBypassServiceWorker', { bypass:true });
  await command('Emulation.setDeviceMetricsOverride', { width:1440, height:1000, deviceScaleFactor:1, mobile:false });
  const preload = await command('Page.addScriptToEvaluateOnNewDocument', { source:`(() => {
    if (!sessionStorage.getItem('__clanker_fresh')) {
      localStorage.setItem('odysseus-theme', JSON.stringify({
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
      }));
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
  await waitFor("document.readyState === 'complete' && document.querySelectorAll('#themeGrid .theme-swatch').length >= 18 && document.getElementById('clanker-kene-weave-canvas')?.dataset.motion === 'active'", 'persisted Signal Weave startup');
  const persistedStartup = await evaluate(`(async () => {
    const first=document.getElementById('clanker-kene-weave-canvas');
    const firstScene=first?.__backgroundScene;
    const firstFrame=first?.__backgroundFrameCount || 0;
    const configurations=new Set();
    let stable=!!first && !!firstScene;
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
        && canvas?.__backgroundScene===firstScene
        && canvas?.dataset.motion==='active';
      await new Promise(resolve=>setTimeout(resolve,50));
    }
    observer.disconnect();
    return {
      stable,
      canvasMutations,
      configurations:configurations.size,
      frameDelta:(first?.__backgroundFrameCount || 0)-firstFrame,
    };
  })()`);
  assert.equal(persistedStartup.stable, true, 'persisted Signal Weave changed canvas, scene, class, or motion state');
  assert.equal(persistedStartup.canvasMutations, 0, 'a second theme module remounted the running vanilla-owned canvas');
  assert.equal(persistedStartup.configurations, 1, 'persisted Signal Weave palette or effect configuration oscillated');
  assert(persistedStartup.frameDelta > 30, `persisted Signal Weave only advanced ${persistedStartup.frameDelta} frames`);
  const paletteFallback = await evaluate(`(async () => {
    const canvas=document.getElementById('clanker-kene-weave-canvas');
    const scene=canvas?.__backgroundScene;
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
    return {
      styles:[...styles],
      stableScene:canvas?.__backgroundScene===scene,
    };
  })()`);
  assert(paletteFallback.styles.length >= 6, `Signal Weave collapsed to ${paletteFallback.styles.join(', ')} without the body theme class`);
  assert.equal(paletteFallback.stableScene, true, 'Signal Weave reset its scene while preserving its palette');
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

  await evaluate("localStorage.removeItem('odysseus-theme')");
  await reloadAndWait("document.readyState === 'complete' && document.querySelectorAll('#themeGrid .theme-swatch').length >= 18 && document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'fresh theme UI');
  await waitFor("document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'active Clanker route field');
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
  const staleOwnerRecovery = await evaluate(`(async () => {
    const original=document.getElementById('clanker-routefield-canvas');
    original.dataset.backgroundRuntime='legacy-runtime';
    document.getElementById('theme-bg-pattern-select').dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const replacement=document.getElementById('clanker-routefield-canvas');
    return {
      originalConnected:original.isConnected,
      replaced:replacement!==original,
      runtime:replacement?.dataset.backgroundRuntime || '',
    };
  })()`);
  assert.equal(staleOwnerRecovery.originalConnected, false, 'stale background owner stayed connected');
  assert.equal(staleOwnerRecovery.replaced, true, 'current runtime did not replace the stale canvas owner');
  assert.notEqual(staleOwnerRecovery.runtime, 'legacy-runtime', 'replacement kept the stale runtime identity');
  const duplicateRecovery = await evaluate(`(async () => {
    const original=document.getElementById('clanker-routefield-canvas');
    const duplicate=document.createElement('canvas');
    duplicate.id='stale-background-canvas';
    duplicate.dataset.backgroundEffectCanvas='true';
    document.body.prepend(duplicate);
    document.getElementById('theme-bg-pattern-select').dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    return {
      originalConnected:original.isConnected,
      duplicateConnected:duplicate.isConnected,
      active:[...document.querySelectorAll('[data-background-effect-canvas]')].map(canvas=>canvas.id),
    };
  })()`);
  assert.equal(duplicateRecovery.originalConnected, false);
  assert.equal(duplicateRecovery.duplicateConnected, false);
  assert.deepEqual(duplicateRecovery.active, ['clanker-routefield-canvas']);
  const teardown = await evaluate(`(async () => {
    const select=document.getElementById('theme-bg-pattern-select');
    const oldCanvas=document.getElementById('clanker-routefield-canvas');
    const oldFrameCount=oldCanvas.__backgroundFrameCount;
    select.value='clanker-radar';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(resolve=>setTimeout(resolve,80));
    const result={
      oldConnected:oldCanvas.isConnected,
      oldFrameDelta:oldCanvas.__backgroundFrameCount-oldFrameCount,
      active:[...document.querySelectorAll('[data-background-effect-canvas]')].map(canvas=>canvas.id),
    };
    select.value='clanker-routefield';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    return result;
  })()`);
  assert.equal(teardown.oldConnected, false);
  assert.equal(teardown.oldFrameDelta, 0, 'detached background canvas kept painting');
  assert.deepEqual(teardown.active, ['clanker-radar-canvas']);
  await waitFor("document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'restored route field');

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
  assert(routeCadence.min >= 7, `route field rendered twice inside a single frame (${routeCadence.min.toFixed(1)}ms)`);
  assert(routeCadence.median < 24, `route field median frame interval was ${routeCadence.median.toFixed(1)}ms`);
  assert(await canvasSceneStable('clanker-routefield-canvas'), 'route field rebuilt its scene without a viewport change');
  assert(await canvasPatternStable('clanker-routefield-canvas'), 'route field rebuilt its scene for an unchanged pattern');
  const routeSafety = await assertCanvasSafe('clanker-routefield-canvas', 'route field');
  await screenshot('clanker-dark-page');
  await screenshot('clanker-dark', 'popup');

  const patternResults = {};
  for (const [pattern, canvasId, screenshotName, minimumPainted] of [
    ['clanker-kene-weave', 'clanker-kene-weave-canvas', 'clanker-kene-weave', 70000],
    ['clanker-radar', 'clanker-radar-canvas', 'clanker-radar', 120000],
    ['clanker-gem-drift', 'clanker-gem-drift-canvas', 'clanker-gem-drift', 12000],
    ['clanker-emoji-drift', 'clanker-emoji-drift-canvas', 'clanker-emoji-drift', 6000],
  ]) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change', {bubbles:true})); return select.value; })()`);
    await waitFor(`document.body.classList.contains('bg-pattern-${pattern}') && document.getElementById('${canvasId}')?.dataset.motion === 'active'`, pattern);
    const frameA = await canvasState(canvasId);
    await new Promise(resolve => setTimeout(resolve, 320));
    const frameB = await canvasState(canvasId);
    assert(frameA?.painted > 0, `${pattern} did not paint`);
    assert(frameA.painted >= minimumPainted, `${pattern} only painted ${frameA.painted} sampled pixels`);
    assert.notEqual(frameA.hash, frameB?.hash, `${pattern} did not animate`);
    const safety = await assertCanvasSafe(canvasId, pattern);
    patternResults[pattern] = { frameA, frameB, safety };
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

    await choose('clanker-kene-weave');
    const keneCanvas=document.getElementById('clanker-kene-weave-canvas');
    const keneScene=keneCanvas?.__backgroundScene;
    const keneKeys=['snakeCount','snakeLengthVariation','snakeLifetimeVariation','shorterLastLonger','longerDisappearSooner'];
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
      stable:document.getElementById('clanker-kene-weave-canvas')===keneCanvas && keneCanvas?.__backgroundScene===keneScene,
    };

    await choose('clanker-gem-drift');
    const gemCanvas=document.getElementById('clanker-gem-drift-canvas');
    const gemScene=gemCanvas?.__backgroundScene;
    const gemSize=control('clanker-gem-drift','gemSizeVariation');
    const gemSizeRange={min:gemSize.min,max:gemSize.max,value:gemSize.value};
    setRange(control('clanker-gem-drift','driftSpeed'), 140);
    setRange(gemSize, 0);
    setRange(gemSize, 999);
    setRange(gemSize, 650);
    setRange(control('clanker-gem-drift','intensityVariation'), 425);
    setRange(control('clanker-gem-drift','middleIntensity'), 120);
    setRange(control('clanker-gem-drift','totalQuantity'), 160);
    setRange(control('clanker-gem-drift','glowLikelihood'), 35);
    await pause();
    const gem={
      controls:['driftSpeed','gemSizeVariation','intensityVariation','middleIntensity','totalQuantity','glowLikelihood'].every(key=>!!control('clanker-gem-drift',key)),
      stable:document.getElementById('clanker-gem-drift-canvas')===gemCanvas && gemCanvas?.__backgroundScene===gemScene,
      sizeRange:gemSizeRange,
    };

    await choose('clanker-emoji-drift');
    const emojiCanvas=document.getElementById('clanker-emoji-drift-canvas');
    const emojiScene=emojiCanvas?.__backgroundScene;
    const emojiSize=control('clanker-emoji-drift','gemSizeVariation');
    const emojiSizeRange={min:emojiSize.min,max:emojiSize.max,value:emojiSize.value};
    setRange(control('clanker-emoji-drift','driftSpeed'), 80);
    setRange(control('clanker-emoji-drift','gemSizeVariation'), 825);
    setRange(control('clanker-emoji-drift','intensityVariation'), 480);
    setRange(control('clanker-emoji-drift','middleIntensity'), 80);
    setRange(control('clanker-emoji-drift','totalQuantity'), 120);
    setRange(control('clanker-emoji-drift','glowLikelihood'), 45);
    await pause();
    const saved=JSON.parse(localStorage.getItem('odysseus-theme'));
    const emoji={
      controls:['driftSpeed','gemSizeVariation','intensityVariation','middleIntensity','totalQuantity','glowLikelihood'].every(key=>!!control('clanker-emoji-drift',key)),
      stable:document.getElementById('clanker-emoji-drift-canvas')===emojiCanvas && emojiCanvas?.__backgroundScene===emojiScene,
      font:document.fonts.check('24px "Noto Color Emoji"'),
      glyphs:new Set(emojiScene?.shards.map(shard=>shard.emoji)).size,
      sizeRange:emojiSizeRange,
    };

    await choose('clanker-radar');
    return { kene, gem, emoji, hidden:document.getElementById('theme-bg-effect-controls')?.hidden, saved, controls:saved?.bgEffectControls };
  })()`);
  assert(effectControlResults.kene.controls);
  assert(effectControlResults.kene.shortScaleAbsent && effectControlResults.kene.longScaleAbsent);
  assert(effectControlResults.kene.shortScale && effectControlResults.kene.longScale);
  assert(effectControlResults.kene.stable, 'Signal Weave controls remounted its canvas');
  assert(effectControlResults.gem.controls && effectControlResults.gem.stable, 'Gem Drift controls remounted its canvas');
  assert.deepEqual(effectControlResults.gem.sizeRange, { min:'0', max:'999', value:'100' });
  assert(effectControlResults.emoji.controls && effectControlResults.emoji.stable, 'Emoji Drift controls remounted its canvas');
  assert.deepEqual(effectControlResults.emoji.sizeRange, { min:'0', max:'999', value:'100' });
  assert(effectControlResults.emoji.font, 'Emoji Drift did not resolve Noto Color Emoji');
  assert(effectControlResults.emoji.glyphs > 8, 'Emoji Drift did not draw a broad emoji range');
  assert(effectControlResults.hidden, 'effects without controls left a stale control panel visible');
  assert(effectControlResults.controls, `effect controls did not persist: ${JSON.stringify(effectControlResults.saved)}`);
  assert.equal(effectControlResults.controls['clanker-kene-weave'].snakeCount, 11);
  assert.equal(effectControlResults.controls['clanker-kene-weave'].shorterLifetimeScale, 150);
  assert.equal(effectControlResults.controls['clanker-kene-weave'].longerLifetimeScale, 55);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].driftSpeed, 140);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].gemSizeVariation, 650);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].intensityVariation, 425);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].middleIntensity, 120);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].totalQuantity, 160);
  assert.equal(effectControlResults.controls['clanker-gem-drift'].glowLikelihood, 35);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].driftSpeed, 80);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].gemSizeVariation, 825);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].intensityVariation, 480);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].middleIntensity, 80);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].totalQuantity, 120);
  assert.equal(effectControlResults.controls['clanker-emoji-drift'].glowLikelihood, 45);
  const extremeSafety = {};
  for (const [pattern, canvasId] of [
    ['clanker-gem-drift', 'clanker-gem-drift-canvas'],
    ['clanker-emoji-drift', 'clanker-emoji-drift-canvas'],
  ]) {
    await evaluate(`(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value=${JSON.stringify(pattern)}; select.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.dataset.motion === 'active'`, `${pattern} extreme safety`);
    extremeSafety[pattern] = await assertCanvasSafe(canvasId, `${pattern} extreme controls`);
  }
  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-kene-weave'; select.dispatchEvent(new Event('change',{bubbles:true})); })()");
  await waitFor("document.getElementById('clanker-kene-weave-canvas')?.dataset.motion === 'active'", 'Signal Weave control screenshot');
  await evaluate("(() => { document.getElementById('theme-modal')?.classList.remove('hidden'); document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]')?.click(); document.getElementById('theme-bg-effect-controls')?.scrollIntoView({block:'center'}); })()");
  await screenshot('clanker-kene-controls', 'popup');
  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-gem-drift'; select.dispatchEvent(new Event('change',{bubbles:true})); document.getElementById('theme-bg-effect-controls')?.scrollIntoView({block:'center'}); })()");
  await waitFor("document.getElementById('clanker-gem-drift-canvas')?.dataset.motion === 'active'", 'Gem Drift control screenshot');
  await screenshot('clanker-gem-controls', 'popup');
  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-emoji-drift'; select.dispatchEvent(new Event('change',{bubbles:true})); document.getElementById('theme-bg-effect-controls')?.scrollIntoView({block:'center'}); })()");
  await waitFor("document.getElementById('clanker-emoji-drift-canvas')?.dataset.motion === 'active'", 'Emoji Drift control screenshot');
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
    setRange(control('clanker-gem-drift','gemSizeVariation'), 100);
    setRange(control('clanker-gem-drift','intensityVariation'), 100);
    setRange(control('clanker-gem-drift','middleIntensity'), 100);
    setRange(control('clanker-gem-drift','totalQuantity'), 100);
    setRange(control('clanker-gem-drift','glowLikelihood'), 11);
    select.value='clanker-emoji-drift';
    select.dispatchEvent(new Event('change',{bubbles:true}));
    setRange(control('clanker-emoji-drift','driftSpeed'), 100);
    setRange(control('clanker-emoji-drift','gemSizeVariation'), 100);
    setRange(control('clanker-emoji-drift','intensityVariation'), 100);
    setRange(control('clanker-emoji-drift','middleIntensity'), 100);
    setRange(control('clanker-emoji-drift','totalQuantity'), 100);
    setRange(control('clanker-emoji-drift','glowLikelihood'), 11);
    select.value='clanker-kene-weave';
    select.dispatchEvent(new Event('change',{bubbles:true}));
  })()`);
  await waitFor("document.getElementById('clanker-kene-weave-canvas')?.dataset.motion === 'active'", 'restored Signal Weave defaults');

  const canvasPatternIds = {
    'clanker-routefield':'clanker-routefield-canvas',
    'clanker-kene-weave':'clanker-kene-weave-canvas',
    'clanker-radar':'clanker-radar-canvas',
    'clanker-gem-drift':'clanker-gem-drift-canvas',
    'clanker-emoji-drift':'clanker-emoji-drift-canvas',
    synapse:'synapse-canvas', rain:'rain-canvas', constellations:'constellations-canvas',
    'perlin-flow':'perlin-flow-canvas', petals:'petals-canvas', sparkles:'sparkles-canvas', embers:'embers-canvas',
  };
  const patternOrder = [
    'none', 'clanker-routefield', 'clanker-kene-weave', 'clanker-radar',
    'clanker-gem-drift', 'clanker-emoji-drift', 'clanker-blueprint', 'dots', 'synapse', 'rain',
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
          && (!expectedCanvas || (canvases[0].id===expectedCanvas && canvases[0].dataset.motion==='active'
            && background.backgroundImage==='none' && background.animationName==='none'));
        if (!valid) failures.push({from,to,classes,canvases:canvases.map(node=>({id:node.id,motion:node.dataset.motion})),backgroundImage:background.backgroundImage,animationName:background.animationName});
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
    await waitFor(`document.getElementById(${JSON.stringify(canvasId)})?.dataset.motion === 'active'`, `${pattern} canvas owner`);
    await new Promise(resolve => setTimeout(resolve, 420));
    const frameA = await canvasState(canvasId);
    await new Promise(resolve => setTimeout(resolve, 360));
    const frameB = await canvasState(canvasId);
    const change = await canvasChange(canvasId, 360);
    assert(frameA?.painted > 0, `${pattern} did not paint`);
    assert.notEqual(frameA.hash, frameB?.hash, `${pattern} did not animate`);
    assert(change?.coverage < 0.08, `${pattern} changed ${Math.round((change?.coverage || 0) * 100)}% of sampled pixels`);
    assert(await canvasSceneStable(canvasId), `${pattern} rebuilt its scene without a viewport change`);
    assert(await canvasPatternStable(canvasId), `${pattern} rebuilt its scene for an unchanged pattern`);
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
    const safety = await assertCanvasSafe(canvasId, `${pattern} mobile`);
    mobilePatternResults[pattern] = { frame, safety };
    await screenshot(`${pattern}-mobile`);
  }

  await evaluate("(() => { const select=document.getElementById('theme-bg-pattern-select'); select.value='clanker-routefield'; select.dispatchEvent(new Event('change',{bubbles:true})); })()");
  await waitFor("document.getElementById('clanker-routefield-canvas')?.dataset.motion === 'active'", 'route field before live DPR resize');
  const beforeDprResize = await evaluate(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    globalThis.__clankerResizeProbe={ canvas, scene:canvas?.__backgroundScene };
    return { resizeCount:canvas?.__backgroundResizeCount || 0 };
  })()`);
  await command('Emulation.setDeviceMetricsOverride', { width:412, height:915, deviceScaleFactor:2, mobile:true });
  await waitFor("innerWidth === 412 && document.getElementById('clanker-routefield-canvas')?.width === 824 && document.getElementById('clanker-routefield-canvas')?.height === 1830", 'live DPR resize');
  const dprResize = await evaluate(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    return {
      sameCanvas:canvas===globalThis.__clankerResizeProbe?.canvas,
      sceneChanged:canvas?.__backgroundScene!==globalThis.__clankerResizeProbe?.scene,
      resizeCount:canvas?.__backgroundResizeCount || 0,
    };
  })()`);
  const dprResizeIdentity = await evaluate(`(() => {
    const canvas=document.getElementById('clanker-routefield-canvas');
    return {
      canvasId:canvas?.id,
      width:canvas?.width,
      height:canvas?.height,
      resizeCount:canvas?.__backgroundResizeCount || 0,
    };
  })()`);
  assert.equal(dprResizeIdentity.canvasId, 'clanker-routefield-canvas');
  assert.equal(dprResize.sameCanvas, true, 'live viewport change remounted the running canvas');
  assert.equal(dprResize.sceneChanged, true, 'live viewport change reused stale scene geometry');
  assert(dprResizeIdentity.resizeCount > beforeDprResize.resizeCount, 'live viewport change did not run the shared resize lifecycle');
  const dprSafety = await assertCanvasSafe('clanker-routefield-canvas', 'route field DPR 2 resize');

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
  process.stdout.write(`${JSON.stringify({ dark, routeStability, routeCadence, routeSafety, vanillaPresentation, patternResults, effectControlResults, extremeSafety, transitionMatrix, effectMotionResults, light, original, fontViews, migration, reducedResults, mobile, mobilePatternResults, dprResize, dprResizeIdentity, dprSafety, login, mobileLogin, screenshots:outputDir }, null, 2)}\n`);
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => chromium.once('exit', resolve));
  fs.rmSync(profile, { recursive:true, force:true });
}
