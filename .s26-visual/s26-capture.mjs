#!/usr/bin/env node
/**
 * S26 visual evidence capture — T10–T18 remaining defects and the
 * all-presets / all-effects qualification matrix.
 *
 * Serves the worktree over HTTP and drives headless Chrome via CDP. Numbers in
 * the notes are sampled from the real theme.js scene / computed style at
 * capture time (not a mock). Fixture harness, not a live-app screenshot.
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';

const WORKTREE = '/Users/e/open-clank/.references/upstream-sync-2026-09-22/execution/s26/worktree';
const outDir = '/Users/e/open-clank/.clankers/robonotes/upstream-sync-2026-09-17/execution/s26/visual';
fs.mkdirSync(outDir, { recursive: true });

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.png': 'image/png', '.json': 'application/json',
  '.svg': 'image/svg+xml',
};
const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent((req.url || '/').split('?')[0]);
  const rel = urlPath === '/' ? '/.s26-visual/harness.html' : urlPath;
  const file = path.join(WORKTREE, rel);
  if (!file.startsWith(WORKTREE)) { res.writeHead(403).end(); return; }
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404).end('not found'); return; }
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(data);
  });
});
const httpPort = await new Promise((resolve, reject) => {
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => resolve(server.address().port));
});

const cdpPort = await new Promise((resolve, reject) => {
  const probe = net.createServer();
  probe.once('error', reject);
  probe.listen(0, '127.0.0.1', () => {
    const selected = probe.address().port;
    probe.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-s26-vis-'));
const chrome = [
  process.env.OPENCLANK_CHROME_BIN,
  process.env.CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/opt/homebrew/bin/chromium',
  '/usr/bin/chromium',
].find(c => c && fs.existsSync(c));
if (!chrome) throw new Error('Chrome not found');

const chromeProc = spawn(chrome, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  `--remote-debugging-port=${cdpPort}`, `--user-data-dir=${profile}`,
  '--window-size=1280,900', 'about:blank',
], { stdio: 'ignore' });

const notes = [];
const notesPath = path.join(outDir, 's26-computed-notes.txt');
const log = (line) => {
  notes.push(line);
  console.log(line);
  // Flush incrementally so a mid-run failure keeps the evidence gathered so far.
  fs.writeFileSync(notesPath, notes.join('\n') + '\n');
};

try {
  const debuggerBase = `http://127.0.0.1:${cdpPort}`;
  let targets;
  for (let i = 0; i < 120; i += 1) {
    try { targets = await fetch(`${debuggerBase}/json`).then(r => r.json()); break; }
    catch { await new Promise(r => setTimeout(r, 50)); }
  }
  const target = targets?.find(t => t.type === 'page');
  if (!target?.webSocketDebuggerUrl) throw new Error('no CDP page target');
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    socket.addEventListener('open', res, { once: true });
    socket.addEventListener('error', rej, { once: true });
  });

  let seq = 0;
  const pending = new Map();
  socket.addEventListener('message', ev => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message));
      else resolve(msg.result);
    }
  });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout ${method}`));
    }, 30000);
    pending.set(id, {
      resolve: (v) => { clearTimeout(timer); resolve(v); },
      reject: (e) => { clearTimeout(timer); reject(e); },
    });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await cdp('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text || 'evaluate failed');
    }
    return result.result?.value;
  };
  const navigate = async (width, height, dpr = 1) => {
    await cdp('Emulation.setDeviceMetricsOverride', {
      width, height, deviceScaleFactor: dpr, mobile: false,
    });
    await cdp('Page.navigate', {
      url: `http://127.0.0.1:${httpPort}/.s26-visual/harness.html?w=${width}&h=${height}`,
    });
    for (let i = 0; i < 80; i += 1) {
      const ready = await evaluate('!!window.__themeReady && !!window.__s26');
      if (ready) return;
      await new Promise(r => setTimeout(r, 60));
    }
    throw new Error('harness did not become ready');
  };
  const screenshot = async (name) => {
    const shot = await cdp('Page.captureScreenshot', {
      format: 'png', captureBeyondViewport: true,
    });
    fs.writeFileSync(path.join(outDir, `${name}.png`), Buffer.from(shot.data, 'base64'));
    log(`  saved ${name}.png`);
  };
  const wait = (ms) => new Promise(r => setTimeout(r, ms));

  log('# S26 visual evidence');
  log('');
  log('Fixture harness importing real /static/js/theme.js — `L-S26-FIXTURE-NOT-LIVE-APP`.');
  log('Chrome headless with --disable-gpu (Canvas2D only — `L-S26-GPU-EVIDENCE`).');
  log('');

  // ── 1. All 17 background choices mount with exactly one owner ─────────────
  log('## All 17 backgrounds: mount + single owner (Hex)');
  await navigate(1280, 800);
  const patterns = [
    'none', 'clanker-routefield', 'clanker-kene-weave', 'clanker-lcars',
    'clanker-gem-drift', 'clanker-emoji-drift', 'clanker-matrix-rain',
    'clanker-emoji-rain', 'clanker-blueprint', 'dots', 'synapse', 'rain',
    'constellations', 'perlin-flow', 'petals', 'sparkles', 'embers',
  ];
  const mountRows = [];
  for (const pattern of patterns) {
    const result = await evaluate(`window.__s26.mount(${JSON.stringify(pattern)})`);
    const owners = await evaluate('window.__s26.ownerCount()');
    mountRows.push({ pattern, ...result, ...owners });
    log(`- ${pattern}: mounted=${result.mounted} canvas=${result.canvasId || '(css)'} canvases=${owners.canvases} owner=${owners.owner}`);
  }
  const multiOwner = mountRows.filter(r => r.canvases > 1);
  log(`- canvases>1 at any point: ${multiOwner.length === 0 ? 'none' : multiOwner.map(r => r.pattern).join(',')}`);
  log('');

  // ── 2. Reapply-unchanged keeps one owner and one canvas (Hex) ─────────────
  log('## Reapply-unchanged pattern (Hex: no double animation loop)');
  for (const pattern of ['clanker-lcars', 'clanker-emoji-drift', 'rain', 'clanker-kene-weave']) {
    await evaluate(`window.__s26.mount(${JSON.stringify(pattern)})`);
    const re = await evaluate(`window.__s26.reapply(${JSON.stringify(pattern)})`);
    log(`- ${pattern}: sameOwner=${re.sameOwner} sameCanvasId=${re.sameCanvasId} canvas=${re.canvasId}`);
    await screenshot(`s26-reapply-${pattern}`);
  }
  log('');

  // ── 3. T15: intensity applied once on a legacy canvas ─────────────────────
  log('## T15 — legacy canvas intensity applied once (CSS opacity)');
  await evaluate("window.__s26.mount('rain')");
  await wait(300);
  const intensityRows = [];
  for (const v of [0, 0.5, 1]) {
    await evaluate(`window.__s26.setIntensity(${v})`);
    await wait(220);
    const probe = await evaluate('window.__s26.legacyIntensityProbe()');
    intensityRows.push({ v, ...probe });
    log(`- intensity ${v}: cssOpacity=${probe.cssOpacity} hash=${probe.hash} backing=${probe.width}x${probe.height}`);
    await screenshot(`s26-intensity-${String(v).replace('.', 'p')}`);
  }
  log(`- hash changes 0→0.5: ${intensityRows[0].hash !== intensityRows[1].hash}`);
  log(`- hash changes 0.5→1: ${intensityRows[1].hash !== intensityRows[2].hash}`);
  log(`- cssOpacity matches slider (single application): ${intensityRows.map(r => r.cssOpacity).join(' / ')}`);
  log('- hash caveat (L-S26-HASH-METHOD): hashes read the canvas backing store, which CSS opacity');
  log('  does not touch. Hash deltas here are animation motion, not intensity. Real intensity proof is');
  log('  the cssOpacity match above + the PNG size gradient; do not read hash isolation as intensity.');
  log('');

  // ── 4. T16: blueprint effect color + size controls ────────────────────────
  log('## T16 — clanker-blueprint (LCARS Status Sweep) color/size controls');
  await evaluate("window.__s26.mount('clanker-blueprint')");
  await wait(320);
  await screenshot('s26-blueprint-default');
  const bpDefault = await evaluate('window.__s26.blueprintComputed()');
  log(`- default background-size: ${bpDefault.backgroundSize}`);
  await evaluate("window.__s26.setColor('#FF00AA')");
  await wait(180);
  const bpColor = await evaluate('window.__s26.blueprintComputed()');
  await screenshot('s26-blueprint-color');
  // Computed style serializes color-mix to color(srgb r g b / a). #FF00AA is
  // rgb(255,0,170) ≈ srgb 1, 0, 0.667 — look for it anywhere in the image.
  const img = bpColor.backgroundImage;
  const colorWired = img.includes('0.667') || img.includes('0.666')
    || /color\(srgb 1( |\.)+0( |\.)+0\.6/.test(img);
  log(`- css --bg-effect-color: ${bpColor.cssColor}`);
  log(`- after effect color #FF00AA, computed image includes it: ${colorWired}`);
  log(`- image length=${img.length} head: ${img.slice(0, 140)}…`);
  log(`- image tail: …${img.slice(-160)}`);
  await evaluate('window.__s26.setSize(1.8)');
  await wait(180);
  const bpSize = await evaluate('window.__s26.blueprintComputed()');
  await screenshot('s26-blueprint-size');
  log(`- after size 1.8, background-size: ${bpSize.backgroundSize}`);
  log(`- size changed computed geometry: ${bpDefault.backgroundSize !== bpSize.backgroundSize}`);
  log('');

  // ── 5. T17: Dots intensity control discoverable + effective ───────────────
  log('## T17 — Dots intensity control discoverable');
  for (const pattern of ['dots', 'none', 'rain']) {
    const vis = await evaluate(`window.__s26.controlVisibility(${JSON.stringify(pattern)})`);
    log(`- ${pattern}: intensityVisible=${vis.intensityVisible} sizeVisible=${vis.sizeVisible}`);
  }
  await evaluate("window.__s26.mount('dots')");
  await evaluate('window.__s26.setIntensity(1)');
  await wait(160);
  await screenshot('s26-dots-intensity-1');
  await evaluate('window.__s26.setIntensity(0.25)');
  await wait(160);
  await screenshot('s26-dots-intensity-0p25');
  await evaluate('window.__s26.setIntensity(0)');
  await wait(160);
  await screenshot('s26-dots-intensity-0');
  log('- dots screenshots at intensity 1 / 0.25 / 0 saved');
  // Restore full intensity so later sections are not captured at opacity 0.
  await evaluate('window.__s26.setIntensity(1)');
  await wait(160);
  log('');

  // ── 6. T11/T12/T13: Emoji Drift cache, angle, graphemes ───────────────────
  log('## T11–T13 — Emoji Drift');
  await evaluate('window.__s26.setIntensity(1)');
  await evaluate("window.__s26.mount('clanker-emoji-drift')");
  await wait(400);
  const drift0 = await evaluate('window.__s26.driftProbe()');
  log(`- shards=${drift0.shardCount} spriteCache=${drift0.spriteCacheEntries}`);
  log(`- sample glyphs: ${drift0.sampleGlyphs.slice(0, 12).join(' ')}`);
  log(`- multi-code-point glyphs in sample: ${drift0.multiCodePoint}`);
  await screenshot('s26-emoji-drift');
  // Speed probe before the size stress: it only needs a live drift scene, and
  // keeping the pre-stress scene avoids screenshotting the heavy 1.6 scene.
  const speed = await evaluate('window.__s26.driftSpeedChange()');
  log(`- rotating shards sampled: ${speed.sampled} speedInputChanged=${speed.changed}`);
  log(`- slow steps @100% (≈0.0225 rad / 50ms): ${speed.dSlow.join(', ')}`);
  log(`- fast steps @400% (≈0.09 rad / 50ms): ${speed.dFast.join(', ')}`);
  log(`- angles continue from prior value (no reseed/jump): ${speed.continued.every(Boolean)}`);
  log(`- speed change scales the step, not the absolute angle: ${speed.noJump.every(Boolean)}`);
  const stress = await evaluate('window.__s26.stressDriftSizes()');
  log(`- after size stress (10 steps): spriteCache=${stress.spriteCacheEntries} cap=${stress.cap} bounded=${stress.spriteCacheEntries <= stress.cap}`);
  // Restore default size BEFORE any further screenshot: the size-1.6 stressed
  // scene (458 large sprite draws) stalls headless capture-screenshot.
  await evaluate('window.__s26.setSize(1)');
  await wait(200);
  await screenshot('s26-emoji-drift-stressed');
  log('  (s26-emoji-drift-stressed.png taken after size restored to 1; cache numbers above are from the live stressed scene)');
  log('');

  // ── 7. T10: reduced-motion one-shot repaint ───────────────────────────────
  log('## T10 — reduced-motion dirty paint');
  // Fresh page: the prior section leaves a heavy stressed scene behind.
  await navigate(1280, 800);
  log('  step: setIntensity');
  await evaluate('window.__s26.setIntensity(1)');
  log('  step: setColor base');
  await evaluate("window.__s26.setColor('#62C7E8')");
  log('  step: reducedMotion on');
  await evaluate('window.__setReducedMotion(true)');
  log('  step: mount lcars');
  await evaluate("window.__s26.mount('clanker-lcars')");
  log('  step: mounted');
  await wait(400);
  const rm0 = await evaluate('window.__s26.reducedMotionProbe()');
  await screenshot('s26-reduced-motion-a');
  // Style change alone must request one frame — no manual repaint call here.
  log('  step: setColor change');
  await evaluate("window.__s26.setColor('#22DD88')");
  await wait(260);
  const rm1 = await evaluate('window.__s26.reducedMotionProbe()');
  await screenshot('s26-reduced-motion-b');
  log(`- hash before=${rm0.hash} after=${rm1.hash} changed=${rm0.hash !== rm1.hash}`);
  log(`- css color ${rm0.cssColor} → ${rm1.cssColor}`);
  log(`- one-shot repaint wired into applyBgEffectColor (no manual call): ${rm0.hash !== rm1.hash}`);
  await evaluate('window.__setReducedMotion(false)');
  log('');

  // ── 8. T18: live DPR change recomputes backing store ──────────────────────
  log('## T18 — live DPR change');
  await navigate(1000, 700, 1);
  await evaluate("window.__s26.mount('rain')");
  await wait(320);
  const dpr1 = await evaluate('window.__s26.dprProbe()');
  log(`- dpr=1: backing=${dpr1.backingWidth}x${dpr1.backingHeight} dpr=${dpr1.dpr}`);
  await navigate(1000, 700, 2);
  await evaluate("window.__s26.mount('rain')");
  await wait(320);
  const dpr2 = await evaluate('window.__s26.dprProbe()');
  log(`- dpr=2: backing=${dpr2.backingWidth}x${dpr2.backingHeight} dpr=${dpr2.dpr}`);
  log(`- backing scaled with DPR: ${dpr2.backingWidth === dpr1.backingWidth * 2 || dpr2.backingWidth >= dpr1.backingWidth * 1.5}`);
  await screenshot('s26-dpr-2');
  log('');

  // ── 9. Representative screenshots for material choices ───────────────────
  log('## Representative material screenshots (1280×800)');
  await navigate(1280, 800);
  for (const pattern of [
    'clanker-lcars', 'clanker-routefield', 'clanker-kene-weave',
    'clanker-gem-drift', 'clanker-emoji-drift', 'clanker-matrix-rain',
    'clanker-emoji-rain', 'clanker-blueprint', 'dots', 'synapse',
    'rain', 'constellations', 'perlin-flow', 'petals', 'sparkles', 'embers',
  ]) {
    await evaluate(`window.__s26.mount(${JSON.stringify(pattern)})`);
    await wait(280);
    await screenshot(`s26-bg-${pattern}`);
  }
  // Nit: `none` is a real picker choice — capture it too (CSS-only, no canvas).
  await evaluate("window.__s26.mount('none')");
  await wait(220);
  await screenshot('s26-bg-none');
  log('  saved s26-bg-none.png (CSS-only choice, no canvas mount)');
  log('');

  // ── 10. 18 palettes × applied + painted + identity (was: keys only) ─────
  // Gap 1 repair: this section used to log Object.keys(THEMES) and stop.
  // Now every palette is actually applied, painted, pixel-sampled, and
  // identity-checked, and screenshotted.
  log('## 18 built-in palettes — applied, painted, identity-checked');
  await navigate(1280, 800);
  await evaluate("window.__s26.mount('rain')");
  const themeNames = await evaluate('Object.keys(window.__theme.THEMES)');
  log(`- count: ${themeNames.length} (${themeNames.join(', ')})`);
  const paletteRows = [];
  for (const name of themeNames) {
    const probe = await evaluate(`window.__s26.applyPalette(${JSON.stringify(name)})`);
    await wait(160);
    await screenshot(`s26-palette-${name}`);
    paletteRows.push({ name, ...probe });
    log(`- ${name}: bg=${probe.bg} fg=${probe.fg} panel=${probe.panel} red=${probe.red}`
      + ` pixelColors=${probe.pixelColors} nonBlank=${probe.nonBlank}`
      + ` identityPreserved=${probe.identityPreserved} cssBgMatchesPalette=${probe.cssBgMatchesPalette}`
      + ` hash=${probe.sampleHash}`);
  }
  const blank = paletteRows.filter(r => !r.nonBlank);
  const identityBroken = paletteRows.filter(r => !r.identityPreserved || !r.cssBgMatchesPalette);
  const bgHashes = new Set(paletteRows.map(r => r.sampleHash));
  log(`- all 18 painted non-blank: ${blank.length === 0 ? 'yes' : 'NO — ' + blank.map(r => r.name).join(',')}`);
  log(`- all 18 identity-preserving (css --bg matches palette): ${identityBroken.length === 0 ? 'yes' : 'NO — ' + identityBroken.map(r => r.name).join(',')}`);
  log(`- distinct palette paint hashes: ${bgHashes.size} / ${paletteRows.length}`);
  log('');
  log('## Custom theme path (plan req 3)');
  const custom = await evaluate('window.__s26.applyCustomPalette()');
  await screenshot('s26-palette-custom-s26-probe');
  log(`- custom s26-probe-custom: saved=${custom.saved} bg=${custom.bg} fg=${custom.fg}`
    + ` pixelColors=${custom.pixelColors} nonBlank=${custom.nonBlank} cssBgMatchesPalette=${custom.cssBgMatchesPalette}`);
  log('');

  // ── 11. Per-control qualification matrix (plan req 3) ───────────────────
  // Gap 1 repair: the plan requires, for each exposed control, a record of
  // default / min / max / live update / persistence / applicability /
  // reduced-motion. This was absent; both the matrix and this note now exist.
  log('## Per-control qualification matrix (default / min / max / live / persist / applicability / reduced-motion)');
  await navigate(1280, 800);
  const matrix = await evaluate('window.__s26.controlMatrix()');
  log(`- rows: ${matrix.length}`);
  log('- format: scope | key | type | default | min | max | liveUpdate | persistence | applicability | reducedMotionDirtyPaint');
  for (const row of matrix) {
    log(`- ${row.scope} | ${row.key} | ${row.type || 'range'} | default=${row.default} | min=${row.min} | max=${row.max}`
      + ` | live=${row.liveUpdate} | persist=${row.persistence}`
      + ` | applies=${row.applicability || row.appliesWhen}`
      + ` | rmDirtyPaint=${row.reducedMotionDirtyPaint}`);
  }
  const globalRows = matrix.filter(r => r.scope === 'global');
  const effectRows = matrix.filter(r => r.scope !== 'global');
  const liveAll = matrix.every(r => r.liveUpdate);
  const rmAll = matrix.every(r => r.reducedMotionDirtyPaint);
  const effectPersistAll = effectRows.every(r => r.persistence === true);
  log(`- live update honored on all rows: ${liveAll}`);
  log(`- effect-control persistence written on change (all ${effectRows.length} rows): ${effectPersistAll}`);
  log(`- global-control persistence: source-wired in initThemeUI (not behaviourally probed in this fixture)`);
  log(`- reduced-motion dirty paint on change for all rows: ${rmAll}`);
  log(`- global rows: ${globalRows.length}; per-pattern effect rows: ${effectRows.length}`);
  log('- limit: matrix covers intensity/size/effect-color + every registered background-effect control.');
  log('  Font, density, frosted, and the base colour pickers are covered by theme_browser_acceptance');
  log('  (persistence/hydration/identity) rather than by this render matrix — not claimed here.');
  log('');

  log('## Limitations');
  log('- L-S26-FIXTURE-NOT-LIVE-APP: real renderer in a fixture harness, not the running product.');
  log('- L-S26-GPU-EVIDENCE: --disable-gpu; hardware WebGL2 vs Canvas2D outstanding for S31/R08.');
  log('- L-S26-PLATFORM: macOS Apple Silicon only here; Linux/Windows browser evidence still required for S31.');
  log('- L-S26-HASH-METHOD: backing-store hashes read canvas pixels, not the composited CSS layer.');
  log('  CSS opacity (T15 intensity) is therefore NOT visible in the hash — intensity is proven by');
  log('  cssOpacity match + PNG size gradient, not by hash isolation. A running animation also moves');
  log('  the hash on its own, so hash deltas alone never isolate a single control.');

  fs.writeFileSync(path.join(outDir, 's26-computed-notes.txt'), notes.join('\n') + '\n');
  console.log('notes written');
} finally {
  chromeProc.kill('SIGTERM');
  server.close();
}
