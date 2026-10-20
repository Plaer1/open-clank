#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const uiOverride = 'export default { showToast() {}, closeAllDropdowns() {} };';
const kenePage = `<!doctype html><html><head><link rel="stylesheet" href="/static/style.css"></head><body><main id="chat-container"></main><script>
  window.__reduceMotion = false;
  window.__rafScheduled = 0;
  window.__rafCallbacks = 0;
  window.__clearRects = 0;
  window.__rafStamps = [];
  window.__frameWork = [];
  window.__inputIssuedAt = 0;
  window.__inputLatencies = [];
  const nativeRaf = window.requestAnimationFrame.bind(window);
  const nativeCancel = window.cancelAnimationFrame.bind(window);
  window.requestAnimationFrame = callback => {
    window.__rafScheduled += 1;
    return nativeRaf(time => {
      const started = performance.now();
      window.__rafCallbacks += 1;
      window.__rafStamps.push(started);
      if (window.__inputIssuedAt) {
        window.__inputLatencies.push(Math.max(0, started - window.__inputIssuedAt));
        window.__inputIssuedAt = 0;
      }
      callback(time);
      window.__frameWork.push(Math.max(0, performance.now() - started));
    });
  };
  window.cancelAnimationFrame = handle => nativeCancel(handle);
  const motion = { get matches() { return window.__reduceMotion; }, addEventListener(type, callback) { if (type === 'change') this.callback = callback; }, removeEventListener() {}, emit() { this.callback?.({ matches:this.matches }); } };
  window.__motion = motion;
  window.matchMedia = query => query.includes('prefers-reduced-motion') ? motion : { matches:false, addEventListener(){}, removeEventListener(){} };
  const originalClearRect = CanvasRenderingContext2D.prototype.clearRect;
  CanvasRenderingContext2D.prototype.clearRect = function(...args) { if (this.canvas?.id === 'clanker-kene-weave-canvas') window.__clearRects += 1; return originalClearRect.apply(this, args); };
  const input = document.createElement('input');
  input.id = 'kene-input-probe';
  input.type = 'range';
  input.addEventListener('input', () => { window.__inputIssuedAt = performance.now(); });
  document.body.append(input);
  window.__hidden = false;
  try { Object.defineProperty(document, 'hidden', { configurable:true, get:() => window.__hidden }); } catch {}
</script><script type="module">
  import * as theme from '/static/js/theme.js';
  window.__theme = theme;
  window.__themeReady = true;
</script></body></html>`;

const pointAt = (route, progress) => {
  const p = Math.max(0, Math.min(1, progress));
  const index = route.cumulative.findIndex(distance => distance >= p * route.total);
  const next = Math.max(1, index < 0 ? route.points.length - 1 : index);
  const a = route.points[next - 1];
  const b = route.points[next];
  const span = Math.max(1, route.cumulative[next] - route.cumulative[next - 1]);
  const local = (p * route.total - route.cumulative[next - 1]) / span;
  return { x:a.x + (b.x - a.x) * local, y:a.y + (b.y - a.y) * local };
};

const percentile = (values, fraction) => {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
};

const summarizeTiming = (intervals, work, inputLatency) => ({
  frames: intervals.length + 1,
  intervalP50: percentile(intervals, .50),
  intervalP95: percentile(intervals, .95),
  intervalMax: Math.max(0, ...intervals),
  workP50: percentile(work, .50),
  workP95: percentile(work, .95),
  workMax: Math.max(0, ...work),
  inputLatencyP50: percentile(inputLatency, .50),
  inputLatencyP95: percentile(inputLatency, .95),
  inputLatencyMax: Math.max(0, ...inputLatency),
});

test('Kene keeps one owner, continuous joins, reduced motion, and bounded frame work', async () => {
  await withCopalBrowser({ page:kenePage, overrides:{ '/static/js/ui.js':uiOverride }, cdpTimeoutMs:45000 }, async ({ cdp, evaluate, until }) => {
    await until('window.__themeReady', 'theme module');
    await evaluate('Math.random = () => 0.0042');
    await evaluate(`window.__reduceMotion=false; window.__theme.applyBgPattern('clanker-kene-weave')`);
    await until("document.getElementById('clanker-kene-weave-canvas')?.__backgroundScene?.snakes?.length >= 1", 'Kene scene');
    await evaluate('new Promise(resolve => setTimeout(resolve, 120))');

    const initial = await evaluate(`(() => { const canvas=document.getElementById('clanker-kene-weave-canvas'); const scene=canvas.__backgroundScene; const snake=scene.snakes[0]; return { mounted:!!canvas, owner:!!window.__openClankBackgroundOwner, raf:window.__rafCallbacks, clears:window.__clearRects, snake:{ routeIndex:snake.routeIndex, reverse:snake.reverse, progress:snake.progress } }; })()`);
    assert.equal(initial.mounted, true, 'Kene canvas did not mount');
    assert.equal(initial.owner, true, 'Kene did not publish a canvas owner');
    const ownerStable = await evaluate(`(() => { const canvas=document.getElementById('clanker-kene-weave-canvas'); const owner=window.__openClankBackgroundOwner; const before=window.__rafScheduled; window.__firstKeneCanvas=canvas; window.__firstKeneOwner=owner; for (let index=0; index<10; index += 1) window.__theme.applyBgPattern('clanker-kene-weave'); return { sameCanvas:canvas === window.__firstKeneCanvas, sameOwner:owner === window.__firstKeneOwner, canvases:document.querySelectorAll('#clanker-kene-weave-canvas').length, scheduledDelta:window.__rafScheduled-before }; })()`);
    assert.deepEqual(ownerStable, { sameCanvas:true, sameOwner:true, canvases:1, scheduledDelta:0 }, 'unchanged Kene reapply changed its owner/canvas/RAF');
    await evaluate('new Promise(resolve => setTimeout(resolve, 180))');
    const cadence = await evaluate('({ raf:window.__rafCallbacks, clears:window.__clearRects })');
    const frameDelta = cadence.raf - initial.raf;
    assert(frameDelta >= 5, `Kene rendered only ${frameDelta} frames`);
    assert.equal(cadence.clears - initial.clears, frameDelta, 'Kene did more than one clear per RAF frame');

    const boundary = await evaluate(`(() => {
      const canvas=document.getElementById('clanker-kene-weave-canvas');
      const scene=canvas.__backgroundScene; const snake=scene.snakes[0];
      const oldRoute=scene.snakeRoutes[0];
      snake.routeIndex=0; snake.reverse=false; snake.progress=15998/16000; snake.lastTime=15998;
      snake.cycle=0; snake.transitionAlpha=0;
      window.__theme.applyBackgroundEffectControls({ 'clanker-kene-weave':{ snakeCount:1, snakeSpeed:100, snakeSpeedVariationToggle:false, snakeLifetimeVariation:0 } });
      canvas.__backgroundPaint(15999, false);
      const before={...scene.heads[0]};
      canvas.__backgroundPaint(16001, false);
      const after={...scene.heads[0]};
      const entryRoute=scene.snakeRoutes[after.routeIndex];
      const entry=after.reverse ? entryRoute.points.at(-1) : entryRoute.points[0];
      const oldEnd=oldRoute.points.at(-1);
      return {
        seed:scene.snakeSeed, before, after,
        cycle:snake.cycle, beforeToOldEnd:Math.hypot(before.x-oldEnd.x, before.y-oldEnd.y),
        joinDistance:Math.hypot(entry.x-oldEnd.x, entry.y-oldEnd.y),
        boundaryDelta:Math.hypot(after.x-before.x, after.y-before.y),
        maxSnakeStep:scene.maxSnakeStep,
      };
    })()`);
    assert.equal(boundary.seed, 42, `Kene fixture seed drifted from 42 (${boundary.seed})`);
    assert.equal(boundary.before.cycle, 0, 'Kene boundary pre-sample already crossed a lifetime');
    assert.equal(boundary.after.cycle, 1, 'Kene 15999/16001ms sample did not cross exactly one lifetime');
    assert(boundary.beforeToOldEnd <= 2, `Kene head was not at the 15999ms route end (${boundary.beforeToOldEnd.toFixed(3)}px)`);
    assert(boundary.joinDistance <= 1e-6, `Kene 16001ms join moved off the connected endpoint (${boundary.joinDistance.toFixed(3)}px)`);
    assert(boundary.boundaryDelta <= Math.max(2, boundary.maxSnakeStep * 3),
      `Kene head teleported at the 15999/16001ms boundary (${boundary.boundaryDelta.toFixed(3)}px)`);

    const timingRuns = [];
    for (const dpr of [1, 2]) {
      await cdp('Emulation.setDeviceMetricsOverride', { width:1440, height:900, deviceScaleFactor:dpr, mobile:false });
      for (const count of [7, 64]) {
        await evaluate(`window.__theme.applyBackgroundEffectControls({ 'clanker-kene-weave':{ snakeCount:${count} } })`);
        await evaluate('new Promise(resolve => setTimeout(resolve, 260))');
        for (let run = 1; run <= 3; run += 1) {
          const timing = await evaluate(`(async () => {
            window.__rafStamps=[]; window.__frameWork=[]; window.__inputLatencies=[];
            document.getElementById('kene-input-probe').dispatchEvent(new Event('input', { bubbles:true }));
            await new Promise(resolve => setTimeout(resolve, 720));
            const stamps=window.__rafStamps.slice();
            return {
              intervals:stamps.slice(1).map((stamp,index)=>stamp-stamps[index]),
              work:window.__frameWork.slice(), inputLatency:window.__inputLatencies.slice(),
            };
          })()`);
          const summary = summarizeTiming(timing.intervals, timing.work, timing.inputLatency);
          assert(summary.frames >= 10, `Kene ${count} snakes DPR${dpr} run ${run} collected only ${summary.frames} frames`);
          assert(timing.inputLatency.length >= 1, `Kene ${count} snakes DPR${dpr} run ${run} recorded no input-to-frame latency`);
          timingRuns.push({ dpr, count, run, ...summary,
            targetP95:summary.intervalP95 <= (count === 7 ? 20 : 33.4),
            targetWorkP95:summary.workP95 <= (count === 7 ? 20 : 33.4),
          });
        }
      }
    }
    // Keep target misses visible in the evidence output. The acceptance target
    // is intentionally measured rather than converted into a synthetic pass.
    console.log(`Kene timing evidence ${JSON.stringify(timingRuns)}`);

    const motionContinuity = await evaluate(`(() => {
      const canvas=document.getElementById('clanker-kene-weave-canvas');
      const scene=canvas.__backgroundScene; const snake=scene.snakes[0];
      const route=scene.snakeRoutes[snake.routeIndex];
      const reset=(progress, lastTime) => { snake.routeIndex=0; snake.reverse=false; snake.progress=progress; snake.lastTime=lastTime; snake.cycle=0; snake.transitionAlpha=0; };
      reset(.25, 1000);
      window.__theme.applyBackgroundEffectControls({ 'clanker-kene-weave':{ snakeSpeed:100, snakeSpeedVariationToggle:false } });
      canvas.__backgroundPaint(1000, false); const beforeSlow={...scene.heads[0]};
      canvas.__backgroundPaint(1100, false); const afterSlow={...scene.heads[0]};
      const slowDistance=Math.hypot(afterSlow.x-beforeSlow.x, afterSlow.y-beforeSlow.y);
      reset(.25, 1000);
      window.__theme.applyBackgroundEffectControls({ 'clanker-kene-weave':{ snakeSpeed:200, snakeSpeedVariationToggle:false } });
      canvas.__backgroundPaint(1000, false); const beforeFast={...scene.heads[0]};
      canvas.__backgroundPaint(1100, false); const afterFast={...scene.heads[0]};
      const fastDistance=Math.hypot(afterFast.x-beforeFast.x, afterFast.y-beforeFast.y);
      return { slowDistance, fastDistance, speedRatio:fastDistance/Math.max(.001, slowDistance), routeLength:route.total };
    })()`);
    assert(motionContinuity.fastDistance > motionContinuity.slowDistance * 1.5,
      `Kene speed change did not move the actual head faster (${JSON.stringify(motionContinuity)})`);
    assert(Number.isFinite(motionContinuity.fastDistance), 'Kene actual head displacement was not finite');

    const join = await evaluate(`(() => {
      const canvas=document.getElementById('clanker-kene-weave-canvas');
      const scene=canvas.__backgroundScene; const snake=scene.snakes[0];
      const oldRoute=scene.snakeRoutes[snake.routeIndex];
      const oldAnchor=snake.reverse ? oldRoute.points[0] : oldRoute.points.at(-1);
      snake.progress=.999; snake.lastTime=performance.now()-80;
      return { oldRoute:snake.routeIndex, oldAnchor, before:{...snake} };
    })()`);
    await until(`document.getElementById('clanker-kene-weave-canvas').__backgroundScene.snakes[0].cycle >= 1`, 'Kene route join');
    const joined = await evaluate(`(() => { const scene=document.getElementById('clanker-kene-weave-canvas').__backgroundScene; const snake=scene.snakes[0]; const route=scene.snakeRoutes[snake.routeIndex]; const entry=snake.reverse ? route.points.at(-1) : route.points[0]; return { cycle:snake.cycle, distance:Math.hypot(entry.x-${join.oldAnchor.x}, entry.y-${join.oldAnchor.y}), routeIndex:snake.routeIndex, progress:snake.progress }; })()`);
    assert(joined.cycle >= 1);
    assert(joined.distance <= 1e-6, `Kene join teleported ${joined.distance.toFixed(2)}px`);

    await evaluate(`window.__reduceMotion=true; window.__motion?.emit()`);
    const reducedBefore = await evaluate('({ raf:window.__rafCallbacks, clears:window.__clearRects })');
    await evaluate('new Promise(resolve => setTimeout(resolve, 180))');
    const reducedAfter = await evaluate('({ raf:window.__rafCallbacks, clears:window.__clearRects })');
    assert.equal(reducedAfter.raf, reducedBefore.raf, 'reduced motion continued scheduling RAF callbacks');
    assert(reducedAfter.clears - reducedBefore.clears <= 1, 'reduced motion repainted repeatedly');

    await evaluate(`window.__theme.applyBgPattern('none')`);
    await until("!document.getElementById('clanker-kene-weave-canvas')", 'Kene disposal');
    assert.equal(await evaluate('window.__openClankBackgroundOwner || null'), null, 'Kene owner survived disposal');
    const disposedRaf = await evaluate('window.__rafCallbacks');
    await evaluate('new Promise(resolve => setTimeout(resolve, 120))');
    assert.equal(await evaluate('window.__rafCallbacks'), disposedRaf, 'disposed Kene continued callbacks');

    const resourceBaseline = await evaluate(`({
      canvases:document.querySelectorAll('[data-background-effect-canvas]').length,
      allCanvases:document.querySelectorAll('canvas').length,
      owner:!!window.__openClankBackgroundOwner,
      host:document.querySelectorAll('.background-effect-host').length,
    })`);
    assert.deepEqual(resourceBaseline, { canvases:0, allCanvases:0, owner:false, host:0 }, 'Kene disposal did not return to its resource baseline');

    await evaluate(`window.__reduceMotion=false; window.__theme.applyBgPattern('clanker-kene-weave')`);
    await until("document.getElementById('clanker-kene-weave-canvas')?.isConnected", 'Kene lifecycle scene');
    // Hide and sample in one browser task: a visible frame may run between
    // separate CDP evaluations and must not count as a hidden callback.
    const hiddenBefore = await evaluate(`(() => { window.__hidden=true; document.dispatchEvent(new Event('visibilitychange')); return { raf:window.__rafCallbacks, canvases:document.querySelectorAll('[data-background-effect-canvas]').length, owner:!!window.__openClankBackgroundOwner }; })()`);
    await evaluate('new Promise(resolve => setTimeout(resolve, 160))');
    const hiddenAfter = await evaluate(`({ raf:window.__rafCallbacks, canvases:document.querySelectorAll('[data-background-effect-canvas]').length, owner:!!window.__openClankBackgroundOwner })`);
    assert.equal(hiddenAfter.raf, hiddenBefore.raf, 'hidden Kene continued RAF callbacks');
    assert.deepEqual(hiddenAfter, hiddenBefore, 'hidden Kene changed canvas/owner resources');
    await evaluate(`window.__hidden=false; document.dispatchEvent(new Event('visibilitychange'))`);
    await until('window.__rafCallbacks > ' + hiddenAfter.raf, 'Kene visibility resume');

    // Repeated pattern switches exercise disposal of RAF, resize and motion
    // listeners. End every cycle at none and compare the exact baseline.
    for (let cycle = 0; cycle < 20; cycle += 1) {
      await evaluate(`window.__theme.applyBgPattern('clanker-lcars')`);
      await until("document.getElementById('clanker-lcars-canvas')?.isConnected", `lcars switch ${cycle + 1}`);
      await evaluate(`window.__theme.applyBgPattern('none')`);
      await until("document.querySelectorAll('[data-background-effect-canvas]').length === 0", `none switch ${cycle + 1}`);
      await evaluate(`window.__theme.applyBgPattern('clanker-kene-weave')`);
      await until("document.getElementById('clanker-kene-weave-canvas')?.isConnected", `Kene switch ${cycle + 1}`);
      await evaluate(`window.__theme.applyBgPattern('none')`);
      await until("document.querySelectorAll('[data-background-effect-canvas]').length === 0", `Kene disposal ${cycle + 1}`);
    }
    const resourceAfterSwitches = await evaluate(`({
      canvases:document.querySelectorAll('[data-background-effect-canvas]').length,
      allCanvases:document.querySelectorAll('canvas').length,
      owner:!!window.__openClankBackgroundOwner,
      host:document.querySelectorAll('.background-effect-host').length,
    })`);
    assert.deepEqual(resourceAfterSwitches, resourceBaseline, '20 Kene pattern switches leaked a canvas/owner/host resource');

    await evaluate(`window.__reduceMotion=false; window.__theme.applyBackgroundEffectControls({ 'clanker-kene-weave':{ snakeCount:64 } }); window.__theme.applyBgPattern('clanker-kene-weave')`);
    await until("document.getElementById('clanker-kene-weave-canvas')?.__backgroundScene?.snakes?.length >= 64", 'Kene stress scene');
    assert.equal(await evaluate("window.__theme.getBackgroundEffectControlValue('clanker-kene-weave', 'snakeCount', 0)"), 64, 'stress measurement did not enable all requested snakes');
    const stressBefore = await evaluate('({ raf:window.__rafCallbacks, clears:window.__clearRects })');
    await evaluate('new Promise(resolve => setTimeout(resolve, 320))');
    const stressAfter = await evaluate('({ raf:window.__rafCallbacks, clears:window.__clearRects })');
    const stressFrames = stressAfter.raf - stressBefore.raf;
    assert(stressFrames >= 5, `Kene stress scene rendered only ${stressFrames} frames`);
    assert.equal(stressAfter.clears - stressBefore.clears, stressFrames, 'stress scene scheduled duplicate canvas clears');
  });
});

const settingsPage = `<!doctype html><html><head><link rel="stylesheet" href="/static/style.css"><style>.sidebar-header{width:100vw;box-sizing:border-box}</style></head><body>
  <div class="sidebar-header"><button class="sidebar-hamburger" id="hamburger" type="button">Menu</button></div>
  <div class="settings-appearance-panel"><div class="admin-card settings-theme-card"><div id="settings-theme-controls"></div></div></div>
  <div id="theme-modal" class="hidden"><div id="theme-popup"><div class="modal-header" id="theme-popup-header">Theme</div><div class="theme-tab-panel"><button id="theme-control" type="button">Theme control</button></div></div></div>
  <script>window.__settingsCalls=[]; window.settingsModule={open(tab){this.opened=tab; window.__settingsCalls.push(['open',tab]);},close(){window.__settingsCalls.push(['close']);}};</script>
  <script type="module">import * as theme from '/static/js/theme.js'; window.__theme=theme; window.__themeReady=true;</script>
</body></html>`;

test('Settings theme bridge preserves keyboard focus and hamburger edge at compact widths', async () => {
  await withCopalBrowser({ page:settingsPage, overrides:{ '/static/js/ui.js':uiOverride } }, async ({ cdp, evaluate, until }) => {
    await until('window.__themeReady', 'theme module');
    await until("document.getElementById('theme-popup').parentElement?.id === 'settings-theme-controls'", 'theme bridge');
    for (const width of [320, 768, 769, 1024, 1025]) {
      await cdp('Emulation.setDeviceMetricsOverride', { width, height:800, deviceScaleFactor:1, mobile:false });
      const state = await evaluate(`(() => { const button=document.getElementById('hamburger'); const style=getComputedStyle(button); const rect=button.getBoundingClientRect(); return { width:innerWidth, left:style.left, right:style.right, position:style.position, rightGap:innerWidth-rect.right }; })()`);
      if (width <= 1024) {
        assert.equal(state.position, 'absolute', `${width}px hamburger lost edge positioning`);
        assert(state.rightGap >= 7 && state.rightGap <= 9, `${width}px hamburger is not at the physical right edge (${state.rightGap}px gap)`);
      }
    }
    await evaluate(`document.getElementById('theme-control').focus(); document.getElementById('theme-modal').classList.remove('hidden')`);
    await until("window.__settingsCalls.some(call => call[0] === 'open')", 'legacy launcher redirect');
    assert.equal(await evaluate('document.activeElement.id'), 'theme-control', 'theme bridge lost keyboard focus');
    assert.equal(await evaluate("document.querySelectorAll('#theme-popup').length"), 1);
  });
});
