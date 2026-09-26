#!/usr/bin/env node
/**
 * S23 graphics substrate unit acceptance (Node, no browser, no WebGL required).
 *
 * Covers: capability fallback, bounded resources, scene state independence,
 * single scene owner, shared consumer lifecycle including context-loss
 * fallback. Honest counts — skipped cases are reported as skips, never as pass.
 */

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BACKEND_WEBGL2,
  BACKEND_CANVAS2D,
  DEFAULT_LIMITS,
  resolveLimits,
  probeGraphicsCapability,
  watchContextLoss,
} from '../static/js/graphics/capability.js';
import {
  createBoundedCache,
  createGlyphAtlas,
  createResourceBudget,
  clampCanvasAllocation,
} from '../static/js/graphics/resources.js';
import { createCanvas2DBackend } from '../static/js/graphics/backend-canvas2d.js';
import { createWebGL2Backend } from '../static/js/graphics/backend-webgl2.js';
import { createGraphicsScene, createDrawBatch } from '../static/js/graphics/scene-state.js';
import {
  createSceneOwner,
  getSceneOwner,
  countSceneOwners,
} from '../static/js/graphics/scene-owner.js';
import { createGraphicsConsumer } from '../static/js/graphics/consumer.js';

function mockCanvas2d() {
  const calls = [];
  const ctx = {
    calls,
    save() { calls.push('save'); },
    restore() { calls.push('restore'); },
    clearRect(...args) { calls.push(['clearRect', ...args]); },
    fillRect(...args) { calls.push(['fillRect', ...args]); },
    beginPath() { calls.push('beginPath'); },
    arc(...args) { calls.push(['arc', ...args]); },
    fill() { calls.push('fill'); },
    stroke() { calls.push('stroke'); },
    moveTo(...args) { calls.push(['moveTo', ...args]); },
    lineTo(...args) { calls.push(['lineTo', ...args]); },
    fillText(...args) { calls.push(['fillText', ...args]); },
    drawImage(...args) { calls.push(['drawImage', ...args]); },
    setTransform(...args) { calls.push(['setTransform', ...args]); },
    measureText(text) { return { width: String(text).length * 7 }; },
    set fillStyle(v) { calls.push(['fillStyle', v]); },
    set strokeStyle(v) { calls.push(['strokeStyle', v]); },
    set lineWidth(v) { calls.push(['lineWidth', v]); },
    set globalAlpha(v) { calls.push(['globalAlpha', v]); },
    set globalCompositeOperation(v) { calls.push(['gco', v]); },
    set font(v) { calls.push(['font', v]); },
    set textAlign(v) { calls.push(['textAlign', v]); },
    set textBaseline(v) { calls.push(['textBaseline', v]); },
    setLineDash(v) { calls.push(['setLineDash', v]); },
  };
  return {
    width: 0,
    height: 0,
    style: {},
    dataset: {},
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
    },
    getContext(kind) {
      if (kind === '2d') return ctx;
      return null;
    },
    setAttribute() {},
    remove() { this.removed = true; },
    __ctx: ctx,
  };
}

function mockWindow({ reduced = false, hidden = false } = {}) {
  const listeners = {};
  const frames = [];
  const win = {
    devicePixelRatio: 1,
    innerWidth: 800,
    innerHeight: 600,
    hidden,
    matchMedia(query) {
      const mql = {
        matches: query.includes('reduced-motion') ? reduced : false,
        media: query,
        listeners: [],
        addEventListener(type, fn) { this.listeners.push(fn); },
        removeEventListener(type, fn) {
          this.listeners = this.listeners.filter((f) => f !== fn);
        },
        addListener(fn) { this.listeners.push(fn); },
        removeListener(fn) {
          this.listeners = this.listeners.filter((f) => f !== fn);
        },
      };
      return mql;
    },
    requestAnimationFrame(fn) {
      const id = frames.length + 1;
      frames.push({ id, fn });
      return id;
    },
    cancelAnimationFrame(id) {
      const idx = frames.findIndex((f) => f.id === id);
      if (idx >= 0) frames.splice(idx, 1);
    },
    performance: { now: () => Date.now() },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
    },
    __frames: frames,
    __listeners: listeners,
    __pump(n = 1) {
      for (let i = 0; i < n; i += 1) {
        const pending = frames.splice(0, frames.length);
        for (const entry of pending) entry.fn(i * 16);
      }
    },
  };
  return win;
}

function mockDocument(hidden = false) {
  const listeners = {};
  return {
    hidden,
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
    },
    createElement(tag) {
      if (tag === 'canvas') return mockCanvas2d();
      return { style: {} };
    },
    __listeners: listeners,
  };
}

test('capability: missing canvas collapses to ordinary fallback', () => {
  const result = probeGraphicsCapability({ canvas: null });
  assert.equal(result.backend, BACKEND_CANVAS2D);
  assert.equal(result.accelerated, false);
  assert.equal(result.reason, 'no-canvas');
  assert.equal(result.webgl2, null);
  assert.equal(result.limits.maxTextureSize, DEFAULT_LIMITS.maxTextureSize);
});

test('capability: forceCanvas2D and disabled WebGL2 never require WebGL', () => {
  const canvas = mockCanvas2d();
  const forced = probeGraphicsCapability({ canvas, forceCanvas2D: true });
  assert.equal(forced.backend, BACKEND_CANVAS2D);
  assert.equal(forced.reason, 'forced-canvas2d');
  const disabled = probeGraphicsCapability({ canvas, allowWebGL2: false });
  assert.equal(disabled.backend, BACKEND_CANVAS2D);
  assert.equal(disabled.reason, 'webgl2-disabled');
});

test('capability: webgl2 throw and null context fall back with reasons', () => {
  const canvas = mockCanvas2d();
  const thrown = probeGraphicsCapability({
    canvas,
    createWebGL2() { throw new DOMException('blocked', 'SecurityError'); },
  });
  assert.equal(thrown.backend, BACKEND_CANVAS2D);
  assert.match(thrown.reason, /^webgl2-throw:/);
  const missing = probeGraphicsCapability({ canvas, createWebGL2: () => null });
  assert.equal(missing.backend, BACKEND_CANVAS2D);
  assert.equal(missing.reason, 'webgl2-unavailable');
});

test('capability: successful webgl2 probe reports accelerated backend and clamped limits', () => {
  const canvas = mockCanvas2d();
  const gl = {
    MAX_TEXTURE_SIZE: 1,
    MAX_RENDERBUFFER_SIZE: 2,
    MAX_VIEWPORT_DIMS: 3,
    getParameter(p) {
      if (p === 1) return 8192;
      if (p === 2) return 8192;
      if (p === 3) return new Int32Array([8192, 8192]);
      return 0;
    },
  };
  const result = probeGraphicsCapability({ canvas, createWebGL2: () => gl });
  assert.equal(result.backend, BACKEND_WEBGL2);
  assert.equal(result.accelerated, true);
  assert.equal(result.webgl2, gl);
  // Device 8192 is clamped to the hard ceiling 2048.
  assert.equal(result.limits.maxTextureSize, DEFAULT_LIMITS.maxTextureSize);
});

test('capability: resolveLimits keeps the smaller of device and ceiling', () => {
  const small = resolveLimits({ maxTextureSize: 512, maxDpr: 1 });
  assert.equal(small.maxTextureSize, 512);
  assert.equal(small.maxDpr, 1);
  const huge = resolveLimits({ maxTextureSize: 65536, maxDpr: 8 });
  assert.equal(huge.maxTextureSize, DEFAULT_LIMITS.maxTextureSize);
  assert.equal(huge.maxDpr, DEFAULT_LIMITS.maxDpr);
});

test('capability: watchContextLoss preventDefaults and detaches cleanly', () => {
  const canvas = mockCanvas2d();
  let lost = 0;
  let restored = 0;
  const detach = watchContextLoss(canvas, {
    onLost: () => { lost += 1; },
    onRestored: () => { restored += 1; },
  });
  const lostEvent = { preventDefault() { this.prevented = true; } };
  for (const fn of canvas.listeners.webglcontextlost || []) fn(lostEvent);
  for (const fn of canvas.listeners.webglcontextrestored || []) fn({});
  assert.equal(lost, 1);
  assert.equal(restored, 1);
  assert.equal(lostEvent.prevented, true);
  detach();
  assert.equal((canvas.listeners.webglcontextlost || []).length, 0);
});

test('resources: bounded cache evicts LRU and disposes entries', () => {
  const disposed = [];
  const cache = createBoundedCache({ maxEntries: 2 });
  cache.set('a', { dispose: () => disposed.push('a') });
  cache.set('b', { dispose: () => disposed.push('b') });
  cache.get('a'); // a becomes most-recent
  cache.set('c', { dispose: () => disposed.push('c') });
  assert.equal(cache.size, 2);
  assert.equal(cache.has('b'), false);
  assert.deepEqual(disposed, ['b']);
  cache.clear();
  assert.deepEqual(disposed.sort(), ['a', 'b', 'c'].sort());
});

test('resources: glyph atlas quantizes sizes and enforces pixel budget', () => {
  const atlas = createGlyphAtlas({ maxEntries: 4, maxPixels: 100 });
  // Ties prefer the lower bucket (11 → 10, 13 → 12).
  assert.equal(atlas.quantizeSize(11), 10);
  assert.equal(atlas.quantizeSize(13), 12);
  assert.equal(atlas.quantizeSize(21), 20);
  assert.equal(atlas.quantizeSize(999), 64);
  atlas.set(atlas.key('A', 12), { uv: 1 }, 60);
  atlas.set(atlas.key('B', 12), { uv: 2 }, 60);
  assert.ok(atlas.pixels <= 100);
  assert.ok(atlas.size <= 4);
});

test('resources: particle and paint budgets reserve, release and reset', () => {
  const budget = createResourceBudget({ maxParticles: 4, maxPaintCells: 3 });
  assert.equal(budget.reserveParticles(3), 3);
  assert.equal(budget.reserveParticles(2), 0);
  budget.releaseParticles(2);
  assert.equal(budget.reserveParticles(1), 1);
  assert.equal(budget.reservePaintCells(3), 3);
  assert.equal(budget.reservePaintCells(1), 0);
  budget.reset();
  assert.equal(budget.particles, 0);
  assert.equal(budget.paintCells, 0);
});

test('resources: clampCanvasAllocation bounds DPR and pixel count', () => {
  const limits = { maxDpr: 2, maxTextureSize: 1024, maxTexturePixels: 1024 * 1024 };
  const ok = clampCanvasAllocation(100, 50, 3, limits);
  assert.equal(ok.dpr, 2);
  assert.equal(ok.width, 200);
  assert.equal(ok.height, 100);
  assert.equal(ok.clamped, false);
  const huge = clampCanvasAllocation(4000, 4000, 2, limits);
  assert.ok(huge.width <= 1024 && huge.height <= 1024);
  assert.equal(huge.clamped, true);
});

test('scene state: style updates do not reset identity; topology rebuild keeps owner-facing id', () => {
  const scene = createGraphicsScene({
    id: 'kene',
    seed: 42,
    width: 100,
    height: 80,
    style: { intensity: 0.5 },
    documentState: { trajectory: [1, 2, 3] },
  });
  const before = scene.snapshot();
  scene.update({ style: { intensity: 0.9 } });
  const after = scene.snapshot();
  assert.equal(after.id, before.id);
  assert.equal(after.seed, before.seed);
  assert.deepEqual(after.documentState.trajectory, [1, 2, 3]);
  assert.equal(after.style.intensity, 0.9);
  scene.advance(16, false);
  assert.ok(scene.phase > 0);
  scene.advance(16, true);
  assert.equal(scene.phase, 0);
  scene.resize(200, 160, 2);
  assert.equal(scene.width, 200);
  assert.equal(scene.dpr, 2);
  // Resize is geometry-only: trajectory survives.
  assert.deepEqual(scene.documentState.trajectory, [1, 2, 3]);
});

test('scene state: draw batch flushes the same list to any backend', () => {
  const seen = [];
  const backend = {
    drawRects(r) { seen.push(['rects', r.length]); },
    drawLines(l) { seen.push(['lines', l.length]); },
    drawCircles(c) { seen.push(['circles', c.length]); },
    drawGlyphs(g) { seen.push(['glyphs', g.length]); },
  };
  const batch = createDrawBatch();
  batch.rect(0, 0, 10, 10, '#fff');
  batch.line(0, 0, 1, 1, '#fff');
  batch.circle(5, 5, 2, '#fff');
  batch.glyph('x', 0, 0, 12, 400, '#fff');
  batch.flush(backend);
  assert.deepEqual(seen, [['rects', 1], ['lines', 1], ['circles', 1], ['glyphs', 1]]);
  assert.equal(batch.size, 0);
});

test('scene owner: one loop per id, dispose cancels frames, reduced motion paints once', () => {
  const host = {};
  const win = mockWindow({ reduced: true });
  const doc = mockDocument(false);
  const paints = [];
  const scene = createGraphicsScene({ id: 's' });
  const owner = createSceneOwner({
    id: 'surface-a',
    scene,
    host,
    win,
    doc,
    paint: (time, reduced) => paints.push({ time, reduced }),
  });
  owner.start();
  assert.equal(countSceneOwners(host), 1);
  assert.equal(getSceneOwner('surface-a', host), owner);
  // Reduced motion: start paints once and schedules no continuous frame.
  assert.equal(paints.length, 1);
  assert.equal(win.__frames.length, 0);
  owner.invalidate();
  assert.equal(paints.length, 2);

  // A second owner with the same id replaces the first without stacking loops.
  const paints2 = [];
  const owner2 = createSceneOwner({
    id: 'surface-a',
    scene,
    host,
    win,
    doc,
    paint: (time, reduced) => paints2.push({ time, reduced }),
  });
  owner2.start();
  assert.equal(owner.disposed, true);
  assert.equal(countSceneOwners(host), 1);
  assert.equal(getSceneOwner('surface-a', host), owner2);

  owner2.dispose();
  assert.equal(countSceneOwners(host), 0);
  assert.equal(win.__frames.length, 0);
});

test('scene owner: animated path schedules exactly one rAF chain and suspends cleanly', () => {
  const host = {};
  const win = mockWindow({ reduced: false });
  const doc = mockDocument(false);
  let paints = 0;
  const owner = createSceneOwner({
    id: 'surface-b',
    host,
    win,
    doc,
    paint: () => { paints += 1; },
  });
  owner.start();
  assert.equal(win.__frames.length, 1);
  owner.invalidate();
  assert.equal(win.__frames.length, 1, 'invalidate must not stack a second loop');
  win.__pump(1);
  assert.equal(paints, 1);
  assert.equal(win.__frames.length, 1, 'frame reschedules exactly one rAF');
  owner.suspend();
  assert.equal(win.__frames.length, 0);
  owner.resume();
  assert.equal(win.__frames.length, 1);
  owner.dispose();
  assert.equal(win.__frames.length, 0);
});

test('consumer: no-WebGL mount uses Canvas2D and keeps scene state across updates', () => {
  const host = {};
  const win = mockWindow({ reduced: false });
  const doc = mockDocument(false);
  const mount = { children: [], appendChild(c) { this.children.push(c); c.parentNode = this; } };
  const paints = [];
  const consumer = createGraphicsConsumer({
    mount,
    id: 'theme-bg',
    win,
    doc,
    ownerHost: host,
    allowWebGL2: true,
    draw: ({ backend, scene, batch }) => {
      paints.push({ kind: backend.kind, id: scene.id });
      batch.rect(0, 0, 10, 10, '#F6BE48');
    },
  });
  assert.equal(consumer.backendKind, BACKEND_CANVAS2D);
  assert.equal(consumer.accelerated, false);
  assert.ok(consumer.fallbackReason);
  win.__pump(1);
  assert.ok(paints.length >= 1);
  assert.equal(paints[0].kind, BACKEND_CANVAS2D);
  const before = consumer.scene.snapshot();
  consumer.update({ style: { intensity: 0.2 } });
  const after = consumer.scene.snapshot();
  assert.equal(after.id, before.id);
  assert.deepEqual(after.documentState, before.documentState);
  consumer.dispose();
  assert.equal(consumer.disposed, true);
  assert.equal(countSceneOwners(host), 0);
});

test('consumer: webgl2 success then context loss falls back with state intact and one owner', () => {
  const host = {};
  const win = mockWindow({ reduced: false });
  const doc = mockDocument(false);
  const mount = {
    children: [],
    appendChild(c) { this.children.push(c); c.parentNode = this; },
    replaceChild(next, prev) {
      const idx = this.children.indexOf(prev);
      if (idx >= 0) this.children[idx] = next;
      else this.children.push(next);
      next.parentNode = this;
      prev.parentNode = null;
      prev.removed = true;
      return prev;
    },
  };
  const gl = {
    MAX_TEXTURE_SIZE: 1,
    MAX_RENDERBUFFER_SIZE: 2,
    MAX_VIEWPORT_DIMS: 3,
    VERTEX_SHADER: 10,
    FRAGMENT_SHADER: 11,
    COMPILE_STATUS: 12,
    LINK_STATUS: 13,
    ARRAY_BUFFER: 14,
    ELEMENT_ARRAY_BUFFER: 15,
    DYNAMIC_DRAW: 16,
    FLOAT: 17,
    TRIANGLES: 18,
    UNSIGNED_SHORT: 19,
    TEXTURE_2D: 20,
    TEXTURE0: 21,
    RGBA: 22,
    UNSIGNED_BYTE: 23,
    TEXTURE_MIN_FILTER: 24,
    TEXTURE_MAG_FILTER: 25,
    TEXTURE_WRAP_S: 26,
    TEXTURE_WRAP_T: 27,
    CLAMP_TO_EDGE: 28,
    LINEAR: 29,
    DEPTH_TEST: 30,
    BLEND: 31,
    SRC_ALPHA: 32,
    ONE_MINUS_SRC_ALPHA: 33,
    ONE: 34,
    ZERO: 35,
    COLOR_BUFFER_BIT: 36,
    getParameter(p) {
      if (p === 1) return 2048;
      if (p === 2) return 2048;
      if (p === 3) return new Int32Array([2048, 2048]);
      return 0;
    },
    getShaderParameter: () => true,
    getProgramParameter: () => true,
    createShader: () => ({}),
    shaderSource() {},
    compileShader() {},
    deleteShader() {},
    createProgram: () => ({}),
    attachShader() {},
    linkProgram() {},
    deleteProgram() {},
    getUniformLocation: () => ({}),
    createVertexArray: () => ({}),
    deleteVertexArray() {},
    createBuffer: () => ({}),
    deleteBuffer() {},
    bindVertexArray() {},
    bindBuffer() {},
    bufferData() {},
    bufferSubData() {},
    enableVertexAttribArray() {},
    vertexAttribPointer() {},
    useProgram() {},
    uniform2f() {},
    uniform1i() {},
    activeTexture() {},
    bindTexture() {},
    createTexture: () => ({}),
    deleteTexture() {},
    texParameteri() {},
    texImage2D() {},
    texSubImage2D() {},
    viewport() {},
    disable() {},
    enable() {},
    blendFuncSeparate() {},
    clearColor() {},
    clear() {},
    drawElements() {},
  };
  let webglAlive = true;
  let webglEverAcquired = false;
  const canvas = mockCanvas2d();
  canvas.width = 64;
  canvas.height = 32;
  canvas.getContext = (kind) => {
    if (kind === 'webgl2') {
      if (webglAlive) webglEverAcquired = true;
      return webglAlive ? gl : null;
    }
    if (kind === '2d') {
      // Real taint rule: a canvas that has ever held a WebGL context can
      // never yield 2D — not before loss, not after. Returning a working 2D
      // context here would skip adoptCanvas and hide a real browser defect.
      if (webglEverAcquired) return null;
      return canvas.__ctx;
    }
    return null;
  };
  mount.appendChild(canvas);
  canvas.parentNode = mount;

  const paints = [];
  const consumer = createGraphicsConsumer({
    mount,
    canvas,
    id: 'graph-edges',
    win,
    doc,
    ownerHost: host,
    draw: ({ backend, scene, batch }) => {
      paints.push(backend.kind);
      batch.line(0, 0, 4, 4, '#F6BE48');
    },
  });

  assert.equal(consumer.backendKind, BACKEND_WEBGL2);
  assert.equal(consumer.accelerated, true);
  assert.equal(countSceneOwners(host), 1);
  const sceneId = consumer.scene.id;
  const before = consumer.scene.snapshot();

  // Paint once on the accelerated backend before loss.
  win.__pump(1);
  assert.ok(paints.includes(BACKEND_WEBGL2));

  // Context loss: state stays, owner stays single, backend becomes Canvas2D.
  webglAlive = false;
  for (const fn of canvas.listeners.webglcontextlost || []) {
    fn({ preventDefault() {} });
  }
  assert.equal(consumer.backendKind, BACKEND_CANVAS2D);
  assert.equal(consumer.accelerated, false);
  assert.equal(consumer.fallbackReason, 'webgl2-context-lost');
  assert.equal(consumer.scene.id, sceneId);
  assert.deepEqual(consumer.scene.documentState, before.documentState);
  assert.equal(countSceneOwners(host), 1);

  // A WebGL-tainted canvas never yields 2D, so adoptCanvas must replace it:
  // new element in the mount, old one detached, dimensions carried over.
  const replaced = consumer.canvas;
  assert.notEqual(replaced, canvas, 'tainted canvas must be replaced');
  assert.equal(canvas.removed, true, 'old canvas detached from the mount');
  assert.equal(replaced.parentNode, mount, 'replacement is mounted');
  assert.ok(mount.children.includes(replaced), 'mount hosts the replacement');
  assert.ok(!mount.children.includes(canvas), 'mount dropped the tainted canvas');
  assert.equal(replaced.width, 64, 'backing-store width carried across the swap');
  assert.equal(replaced.height, 32, 'backing-store height carried across the swap');
  assert.ok(replaced.getContext('2d'), 'replacement can yield a 2D context');

  win.__pump(1);
  assert.ok(paints.includes(BACKEND_WEBGL2));
  assert.ok(paints.includes(BACKEND_CANVAS2D));
  consumer.dispose();
  assert.equal(countSceneOwners(host), 0);
});

test('canvas2d backend exposes the shared adapter surface and draws without throwing', () => {
  const canvas = mockCanvas2d();
  const backend = createCanvas2DBackend({ canvas, limits: DEFAULT_LIMITS });
  assert.equal(backend.kind, 'canvas2d');
  assert.equal(backend.accelerated, false);
  for (const method of [
    'resize', 'beginFrame', 'clear', 'setAlpha', 'setComposite',
    'drawRects', 'drawLines', 'drawCircles', 'drawGlyphs', 'drawImage',
    'endFrame', 'dispose',
  ]) {
    assert.equal(typeof backend[method], 'function', `${method} on canvas2d`);
  }
  backend.resize(20, 10, 1);
  backend.beginFrame();
  backend.clear(0, 0, 0, 0);
  backend.drawRects([{ x: 0, y: 0, w: 2, h: 2, color: '#fff', alpha: 1 }]);
  backend.drawLines([{ x1: 0, y1: 0, x2: 2, y2: 2, color: '#fff', width: 1 }]);
  backend.drawCircles([{ x: 1, y: 1, r: 1, color: '#fff' }]);
  backend.drawGlyphs([{ text: 'A', x: 0, y: 4, size: 12, color: '#fff' }]);
  backend.endFrame();
  backend.dispose();
  assert.equal(backend.disposed, true);
});

test('webgl2 backend: missing context throws so callers can fall back', () => {
  const canvas = mockCanvas2d();
  assert.throws(() => createWebGL2Backend({ canvas }), /webgl2 context unavailable/);
});

test('webgl2 and canvas2d adapters share the consumer-facing method surface', () => {
  const canvas = mockCanvas2d();
  const gl = {
    getParameter: () => 0,
    getShaderParameter: () => true,
    getProgramParameter: () => true,
    createShader: () => ({}),
    shaderSource() {},
    compileShader() {},
    deleteShader() {},
    createProgram: () => ({}),
    attachShader() {},
    linkProgram() {},
    deleteProgram() {},
    getUniformLocation: () => ({}),
    createVertexArray: () => ({}),
    deleteVertexArray() {},
    createBuffer: () => ({}),
    deleteBuffer() {},
    bindVertexArray() {},
    bindBuffer() {},
    bufferData() {},
    bufferSubData() {},
    enableVertexAttribArray() {},
    vertexAttribPointer() {},
    useProgram() {},
    uniform2f() {},
    uniform1i() {},
    activeTexture() {},
    bindTexture() {},
    createTexture: () => ({}),
    deleteTexture() {},
    texParameteri() {},
    texImage2D() {},
    viewport() {},
    disable() {},
    enable() {},
    blendFuncSeparate() {},
    clearColor() {},
    clear() {},
    drawElements() {},
  };
  const webgl = createWebGL2Backend({ canvas, limits: DEFAULT_LIMITS, gl });
  const canvas2d = createCanvas2DBackend({ canvas: mockCanvas2d(), limits: DEFAULT_LIMITS });
  const surface = [
    'resize', 'beginFrame', 'clear', 'setAlpha', 'setComposite',
    'drawRects', 'drawLines', 'drawCircles', 'drawGlyphs', 'drawImage',
    'endFrame', 'dispose',
  ];
  for (const method of surface) {
    assert.equal(typeof webgl[method], 'function', `${method} on webgl2`);
    assert.equal(typeof canvas2d[method], 'function', `${method} on canvas2d`);
  }
  webgl.beginFrame();
  webgl.drawRects([{ x: 0, y: 0, w: 1, h: 1, color: '#F6BE48', alpha: 1 }]);
  webgl.drawLines([{ x1: 0, y1: 0, x2: 2, y2: 2, color: '#fff', width: 1 }]);
  webgl.drawCircles([{ x: 1, y: 1, r: 1, color: '#fff', stroke: '#000', strokeWidth: 1 }]);
  webgl.endFrame();
  webgl.dispose();
  assert.equal(webgl.disposed, true);
});
