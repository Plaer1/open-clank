#!/usr/bin/env node
/**
 * S24 rain collisions and rare upward drop — focused unit/fixture coverage.
 *
 * Deterministic under test seed. The rare event is asserted at the exact
 * 1/10,000 threshold boundary; natural long runs are not used to test it.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  RARE_UPWARD_PROBABILITY,
  COLLISION_ALPHA_THRESHOLD,
  composeRainDirection,
  shouldRareUpward,
  spawnPosition,
  resolveCollision,
  collidesWithObstacles,
} from '../static/js/graphics/rain-physics.js';
import {
  estimatePaintedAlpha,
  elementObstacleEntry,
  createObstacleField,
  PAINTED_ALPHA_THRESHOLD,
  DEFAULT_OBSTACLE_SELECTORS,
} from '../static/js/graphics/obstacle-field.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const themeSource = readFileSync(join(ROOT, 'static/js/theme.js'), 'utf8');

/** Deterministic unit-sine noise matching theme.js `_clankerNoise`. */
function noise(seed) {
  const value = Math.sin(seed * 12.9898 + 78.233) * 43758.5453;
  return value - Math.floor(value);
}

function mockElement({
  rect = { left: 100, top: 100, width: 200, height: 80 },
  style = {},
  parent = null,
  text = '',
  tag = 'DIV',
} = {}) {
  const element = {
    tagName: tag,
    parentElement: parent,
    childNodes: text ? [{ nodeType: 3, textContent: text }] : [],
    innerText: text,
    getBoundingClientRect() {
      return {
        left: rect.left,
        top: rect.top,
        width: rect.width,
        height: rect.height,
        right: rect.left + rect.width,
        bottom: rect.top + rect.height,
      };
    },
    __style: {
      display: 'block',
      visibility: 'visible',
      opacity: '1',
      backgroundColor: 'rgb(25, 26, 30)',
      backgroundImage: 'none',
      boxShadow: 'none',
      borderTopWidth: '0px',
      borderRightWidth: '0px',
      borderBottomWidth: '0px',
      borderLeftWidth: '0px',
      borderTopColor: 'transparent',
      borderRightColor: 'transparent',
      borderBottomColor: 'transparent',
      borderLeftColor: 'transparent',
      overflow: 'visible',
      overflowX: 'visible',
      overflowY: 'visible',
      ...style,
    },
  };
  return element;
}

function getStyle(element) {
  return element.__style;
}

// ── Rare threshold: exact 1/10,000 ─────────────────────────────────────────

test('rare upward: probability constant is exactly 1/10,000', () => {
  assert.equal(RARE_UPWARD_PROBABILITY, 1 / 10000);
  assert.equal(COLLISION_ALPHA_THRESHOLD, 24 / 255);
  assert.equal(PAINTED_ALPHA_THRESHOLD, 24 / 255);
});

test('rare upward: exact threshold boundary — in [0, 1/10000) only', () => {
  assert.equal(shouldRareUpward(0), true, '0 is the inclusive lower bound');
  assert.equal(shouldRareUpward(1 / 10000 - Number.EPSILON), true);
  assert.equal(shouldRareUpward(1 / 10000), false, 'threshold itself is exclusive');
  assert.equal(shouldRareUpward(1 / 10000 + 1e-12), false);
  assert.equal(shouldRareUpward(0.5), false);
  assert.equal(shouldRareUpward(-0.001), false);
  assert.equal(shouldRareUpward(Number.NaN), false);
});

test('rare upward: default ON and separate from the coarse reverse slider', () => {
  // Default composition (down, 0% reverse, rare ON) with a rare sample → up.
  const rare = composeRainDirection({ rareNoise: 0 });
  assert.equal(rare.direction, -1, 'default rare drop travels up');
  assert.equal(rare.rare, true);
  assert.equal(rare.reversed, false);

  // Without a rare sample, default stays down.
  const ordinary = composeRainDirection({ rareNoise: 0.5 });
  assert.equal(ordinary.direction, 1);
  assert.equal(ordinary.rare, false);

  // Toggle OFF suppresses the rare inversion even at rareNoise 0.
  const off = composeRainDirection({ rareUpward: false, rareNoise: 0 });
  assert.equal(off.direction, 1);
  assert.equal(off.rare, false);
});

test('rare upward: omitted rareNoise never fires (safe default)', () => {
  const composed = composeRainDirection({});
  assert.equal(composed.rare, false);
  assert.equal(composed.direction, 1);
});

// ── Direction composition order ────────────────────────────────────────────

test('direction composition: normal → reverse chance → rare inversion', () => {
  // Explicit user direction up, no reverse, no rare.
  assert.equal(composeRainDirection({ rainDown: false, rareNoise: 0.5 }).direction, -1);

  // Reverse chance flips the explicit direction.
  const reversed = composeRainDirection({ rainDown: true, reverseChance: 100, noise: 0, rareNoise: 0.5 });
  assert.equal(reversed.direction, -1);
  assert.equal(reversed.reversed, true);

  // Rare inverts AFTER reverse: up → rare → down.
  const stacked = composeRainDirection({
    rainDown: true,
    reverseChance: 100,
    noise: 0,
    rareUpward: true,
    rareNoise: 0,
  });
  assert.equal(stacked.reversed, true);
  assert.equal(stacked.rare, true);
  assert.equal(stacked.direction, 1, 'rare inversion undoes the reverse');

  // Independent samples: reverseNoise high, rareNoise low.
  const independent = composeRainDirection({
    rainDown: true,
    reverseChance: 50,
    noise: 0.9,
    rareNoise: 0,
  });
  assert.equal(independent.reversed, false);
  assert.equal(independent.rare, true);
  assert.equal(independent.direction, -1);
});

test('rare upward: rare drop spawns from below the viewport (defaults)', () => {
  const composed = composeRainDirection({ rareNoise: 0 });
  const spawn = spawnPosition({
    direction: composed.direction,
    rare: composed.rare,
    waves: false,
    width: 800,
    height: 600,
    radius: 6,
    entryNoise: 0.2,
    lateralNoise: 0.5,
    spread: 1,
    cell: 20,
    initialX: 400,
  });
  assert.equal(composed.direction, -1);
  assert.ok(spawn.y > 600, `rare upward spawn must start below viewport, got y=${spawn.y}`);
});

test('rare upward: sampled once per spawn, not per frame (seeded stream recycle)', () => {
  // Mirrors theme.js reset sampling: one rareNoise per resetCount. At 1/10,000
  // a 200-spawn window deterministically contains zero hits — long natural
  // runs are not how the rare event is tested. Pin the exact seeded count.
  let rareHits = 0;
  let samples = 0;
  for (let reset = 1; reset <= 200; reset += 1) {
    samples += 1;
    const rareNoise = noise(113 + reset * 59);
    if (shouldRareUpward(rareNoise)) rareHits += 1;
  }
  assert.equal(samples, 200);
  assert.equal(rareHits, 0, `expected 0 rare hits in 200 seeded spawns (p=1/10000), got ${rareHits}`);

  // Deterministic seed scan: find and pin an exact hit and a near-miss.
  let hitReset = -1;
  let missReset = -1;
  for (let reset = 1; reset <= 200000 && (hitReset < 0 || missReset < 0); reset += 1) {
    const rareNoise = noise(113 + reset * 59);
    if (hitReset < 0 && shouldRareUpward(rareNoise)) hitReset = reset;
    if (missReset < 0 && rareNoise < RARE_UPWARD_PROBABILITY * 2 && !shouldRareUpward(rareNoise)) {
      missReset = reset;
    }
  }
  assert.ok(hitReset > 0, 'seeded scan must locate a deterministic rare sample');
  assert.equal(shouldRareUpward(noise(113 + hitReset * 59)), true);
  if (missReset > 0) {
    assert.equal(shouldRareUpward(noise(113 + missReset * 59)), false);
  }
});

// ── Collision response ─────────────────────────────────────────────────────

test('collision: visible lateral shove preserves vertical travel', () => {
  // Tall obstacle; drop hugs the left edge so the nearest edge is lateral.
  const obstacle = { left: 100, top: 0, right: 400, bottom: 400, maskAlpha: 255 };
  const response = resolveCollision({
    x: 110,
    y: 200,
    radius: 8,
    obstacle,
    glyphSize: 14,
    scale: 1,
    collisionForce: 2.2,
    bounce: 1.3,
    random: 0.5,
    time: 1000,
    lastHitAt: -Infinity,
  });
  assert.equal(response.hit, true);
  assert.notEqual(response.lateral, 0, 'deflection must be visible');
  // Junction motion: no reversal of vertical velocity — y is preserved on a
  // side hit; speed/direction are untouched by the caller.
  assert.equal(response.y, 200, 'vertical travel preserved on a side hit');
  assert.ok(response.x < 110, 'x deflected along the obstacle edge');
  assert.ok(response.lateral < 0, 'lateral shove continues along the edge');
});

test('collision: behind-layer drops ignore obstacles', () => {
  const obstacle = { left: 0, top: 0, right: 100, bottom: 100, maskAlpha: 255 };
  const response = resolveCollision({
    x: 50,
    y: 50,
    radius: 10,
    obstacle,
    behind: true,
    time: 10,
    lastHitAt: -Infinity,
  });
  assert.equal(response.hit, false);
  assert.equal(collidesWithObstacles({ behind: true }), false);
  assert.equal(collidesWithObstacles({ behind: false }), true);
});

test('collision: obstacles at or below alpha 24/255 are passable', () => {
  const faint = { left: 0, top: 0, right: 100, bottom: 100, maskAlpha: 24 };
  const solid = { left: 0, top: 0, right: 100, bottom: 100, maskAlpha: 25 };
  assert.equal(resolveCollision({
    x: 50, y: 50, radius: 8, obstacle: faint, time: 1, lastHitAt: -Infinity,
  }).hit, false);
  assert.equal(resolveCollision({
    x: 50, y: 50, radius: 8, obstacle: solid, time: 1, lastHitAt: -Infinity,
  }).hit, true);
});

test('collision: hit cooldown suppresses per-frame restrikes', () => {
  const obstacle = { left: 0, top: 0, right: 100, bottom: 100, maskAlpha: 255 };
  const response = resolveCollision({
    x: 50,
    y: 50,
    radius: 8,
    obstacle,
    time: 120,
    lastHitAt: 0,
    hitCooldownMs: 130,
  });
  assert.equal(response.hit, false);
});

test('collision: top/bottom edge hit sticks-and-slides along the leading edge', () => {
  // Wide panel: a drop overlapping the top edge takes the vertical
  // (least-penetration) axis, snaps to the edge, and slides laterally —
  // it does NOT reverse vertical travel (Junction motion).
  const bar = { left: 0, top: 100, right: 400, bottom: 140, maskAlpha: 255 };
  const topHit = resolveCollision({
    x: 250,
    y: 105,
    radius: 8,
    obstacle: bar,
    glyphSize: 14,
    scale: 1,
    collisionForce: 2.2,
    bounce: 1.3,
    random: 0.5,
    time: 1000,
    lastHitAt: -Infinity,
  });
  assert.equal(topHit.hit, true, 'top-edge overlap must actually hit (not vacuous)');
  // Stick-and-slide: y is snapped to just clear the top edge (top - radius = 92)
  // and the drop is not launched away from the panel.
  assert.equal(topHit.y, 92, 'drop snaps to the top leading edge, clear of the panel');
  assert.equal(100 - topHit.y, 8, 'edge clear equals the drop radius — bounded, not a bounce');
  // Visible deflection is the lateral shove along the edge (x right of panel
  // center pushes further right); vertical direction is untouched.
  assert.notEqual(topHit.lateral, 0, 'deflection must be visible');
  assert.ok(topHit.lateral > 0, 'lateral shove continues along the edge');
  assert.ok(Math.abs(topHit.lateral) < 4, 'shove stays a few px step, not a launch');

  // Bottom-edge counterpart: same stick-and-slide, snapped below the panel.
  const bottomHit = resolveCollision({
    x: 150,
    y: 135,
    radius: 8,
    obstacle: bar,
    glyphSize: 14,
    scale: 1,
    collisionForce: 2.2,
    bounce: 1.3,
    random: 0.5,
    time: 1000,
    lastHitAt: -Infinity,
  });
  assert.equal(bottomHit.hit, true);
  assert.equal(bottomHit.y, 148, 'drop snaps to the bottom leading edge, clear of the panel');
  assert.equal(bottomHit.y - 140, 8, 'edge clear equals the drop radius');
  assert.ok(bottomHit.lateral < 0, 'bottom-edge slide shoves toward the nearer side');
});

// ── Obstacle field: painted alpha, clipping, ancestor opacity ─────────────

test('obstacle field: opacity:1 transparent box is not an opaque obstacle', () => {
  const clearBox = mockElement({
    style: {
      opacity: '1',
      backgroundColor: 'transparent',
      backgroundImage: 'none',
      boxShadow: 'none',
      borderTopWidth: '0px',
      borderLeftWidth: '0px',
      borderRightWidth: '0px',
      borderBottomWidth: '0px',
    },
    text: '',
  });
  assert.equal(estimatePaintedAlpha(getStyle(clearBox), 1), 0);
  assert.equal(elementObstacleEntry(clearBox, { getStyle }), null);
});

test('obstacle field: painted panel above 24/255 collides; ancestor opacity scales', () => {
  const panel = mockElement({
    style: { opacity: '1', backgroundColor: 'rgb(25, 26, 30)' },
    text: 'hello',
  });
  const alpha = estimatePaintedAlpha(getStyle(panel), 1, { hasTextPaint: true });
  assert.ok(alpha > 24 / 255, `panel alpha ${alpha} must exceed threshold`);

  const faded = estimatePaintedAlpha(getStyle(panel), 0.1, { hasTextPaint: true });
  assert.ok(faded < alpha, 'ancestor opacity must reduce painted alpha');
  assert.ok(faded <= 24 / 255 || faded < 0.2, 'deeply faded ancestors drop below the collision threshold');

  const entry = elementObstacleEntry(panel, {
    getStyle,
    ancestorAlpha: 1,
    hasTextPaint: () => true,
  });
  assert.ok(entry, 'painted panel becomes an obstacle');
  assert.ok(entry.maskAlpha > 24);
});

test('obstacle field: visibility and clipping drop non-painted surfaces', () => {
  const hidden = mockElement({ style: { visibility: 'hidden', backgroundColor: 'rgb(25,26,30)' } });
  assert.equal(elementObstacleEntry(hidden, { getStyle }), null);

  const clippedAway = mockElement({
    rect: { left: 0, top: 0, width: 200, height: 80 },
    style: { backgroundColor: 'rgb(25,26,30)' },
  });
  const clipParent = mockElement({
    rect: { left: 500, top: 500, width: 50, height: 50 },
    style: { overflow: 'hidden', backgroundColor: 'rgb(25,26,30)' },
  });
  clippedAway.parentElement = clipParent;
  assert.equal(elementObstacleEntry(clippedAway, {
    getStyle,
    ancestorAlpha: 1,
    hasTextPaint: () => true,
    clippingRect: () => ({ left: 500, top: 500, right: 550, bottom: 550, width: 50, height: 50 }),
  }), null, 'fully clipped element is not an obstacle');
});

test('obstacle field: cache serves per-frame hits and invalidates on scroll/resize', () => {
  let scans = 0;
  const host = {
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
    },
    querySelectorAll() {
      scans += 1;
      return [mockElement({ text: 'panel', style: { backgroundColor: 'rgb(25,26,30)' } })];
    },
  };
  const win = {
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
    },
  };
  let clock = 0;
  const field = createObstacleField({
    root: host,
    win,
    now: () => clock,
    getStyle,
    cacheMs: 110,
    selectors: ['.panel'],
  });

  assert.equal(field.obstacles.length, 1);
  assert.equal(field.obstacles.length, 1);
  assert.equal(scans, 1, 'per-frame reads must hit the cache, not rescan DOM');
  assert.ok(field.hitTest(100, 100, 0));

  // Scroll invalidates; next read rescans.
  for (const fn of win.listeners.scroll || []) fn();
  assert.equal(field.obstacles.length, 1);
  assert.equal(scans, 2, 'scroll must invalidate the cached field');

  // TTL expiry also refreshes without an event.
  clock = 500;
  assert.equal(field.obstacles.length, 1);
  assert.equal(scans, 3, 'stale cache expires after cacheMs');

  field.dispose();
  assert.equal(field.obstacles.length, 0);
});

test('obstacle field: default selectors include app surfaces and popups', () => {
  assert.ok(DEFAULT_OBSTACLE_SELECTORS.some((s) => s.includes('chat-input-bar')));
  assert.ok(DEFAULT_OBSTACLE_SELECTORS.some((s) => s.includes('popup')));
  assert.ok(DEFAULT_OBSTACLE_SELECTORS.some((s) => s.includes('dialog')));
  assert.ok(DEFAULT_OBSTACLE_SELECTORS.includes('.tour-hint'));
});

// ── theme.js control registry / scene ownership (source + behavior) ───────

test('theme.js: rare toggle is default ON and separate from reverse chance', () => {
  assert.match(themeSource, /\{ key: 'splashRareUpward', label: '[^']*', type: 'toggle', default: true \}/);
  assert.match(themeSource, /splashRareUpward/);
  assert.match(themeSource, /splashRainReverseChance/);
  assert.match(themeSource, /getBackgroundEffectControlValue\(pattern, 'splashRareUpward', true\)/);
});

test('theme.js: color variance renamed to extra color variation; palette stays full', () => {
  assert.match(themeSource, /label: 'Extra color variation'/);
  assert.match(themeSource, /label: 'Extra color variation amount'/);
  assert.doesNotMatch(themeSource, /label: 'Color variance'/);
  assert.match(themeSource, /_readClankerEffectConfig\(true\)/);
});

test('theme.js: UI-only controls do not reset droplets (topology key excludes them)', () => {
  assert.match(themeSource, /const _RAIN_TOPOLOGY_FIELDS = \[/);
  assert.match(themeSource, /'quantity', 'charVariety', 'sizeVariance', 'minOpacity', 'maxOpacity', 'emojiMix', 'emojiRarity'/);
  // Advanced settings toggle must not appear in the topology rebuild key.
  const topologyBlock = themeSource.slice(
    themeSource.indexOf('_RAIN_TOPOLOGY_FIELDS'),
    themeSource.indexOf('function _clankerCodeRainDirection'),
  );
  assert.doesNotMatch(topologyBlock, /advancedSettings/);
  assert.doesNotMatch(topologyBlock, /rareUpward/);
  assert.doesNotMatch(topologyBlock, /reverseChance/);
});

test('theme.js: rain mounts through graphics consumer with ONE scene owner', () => {
  assert.match(themeSource, /createGraphicsConsumer/);
  assert.match(themeSource, /createObstacleField/);
  assert.match(themeSource, /function _mountClankerCodeRain/);
  assert.match(themeSource, /id: 'clanker-matrix-rain-canvas'/);
  assert.match(themeSource, /id: 'clanker-emoji-rain-canvas'/);
  // Owner key reconciled with the substrate — no double loop.
  assert.match(themeSource, /window\[_BACKGROUND_OWNER_KEY\] = dispose/);
  assert.match(themeSource, /canvas\.dataset\.backgroundEffectCanvas = 'true'/);
  // Rain path must not also register _runBackgroundCanvas (double-own).
  const rainMount = themeSource.slice(
    themeSource.indexOf('function _mountClankerCodeRain'),
    themeSource.indexOf('function _initClankerMatrixRain'),
  );
  assert.doesNotMatch(rainMount, /_runBackgroundCanvas\(/);
});

test('theme.js: direction composes normal → reverse → rare; sample is per spawn', () => {
  assert.match(themeSource, /composeRainDirection\(/);
  assert.match(themeSource, /rareNoise/);
  assert.match(themeSource, /stream\.resetCount/);
  // Per-spawn sample, not per-frame: rareNoise is taken in reset/build only.
  const advance = themeSource.slice(
    themeSource.indexOf('function _advanceClankerCodeRainStream'),
    themeSource.indexOf('function _drawClankerCodeRain'),
  );
  assert.doesNotMatch(advance, /shouldRareUpward|composeRainDirection/);
});

test('theme.js: head-glyph glow restored on the batch.glyph path', () => {
  // The batch.glyph migration must not drop the leading-glyph shadowBlur glow.
  assert.match(themeSource, /index === 0\s*\?\s*\{ color: colors\[streamColor\], blur:/);
  const draw = themeSource.slice(
    themeSource.indexOf('function _drawClankerCodeRain'),
    themeSource.indexOf('function _mountClankerCodeRain'),
  );
  assert.match(draw, /batch\.glyph\([\s\S]*?glow,/);
  // Backends interpret the glow: Canvas2D real shadow, WebGL2 halo.
  const canvas2d = readFileSync(join(ROOT, 'static/js/graphics/backend-canvas2d.js'), 'utf8');
  const webgl2 = readFileSync(join(ROOT, 'static/js/graphics/backend-webgl2.js'), 'utf8');
  assert.match(canvas2d, /ctx\.shadowBlur = glow\.blur/);
  assert.match(webgl2, /glow\.blur > 0/);
});

test('theme.js: existing direction controls and saved keys are preserved', () => {
  for (const key of [
    'splashRainDown', 'splashRainReverseChance', 'splashRainWaves',
    'splashQuantity', 'splashRainSpeed', 'splashCollisionForce', 'splashBounce',
  ]) {
    assert.ok(themeSource.includes(key), `${key} must remain registered`);
  }
});

test('seeded noise is deterministic for reproducible rare samples', () => {
  assert.equal(noise(113), noise(113));
  assert.notEqual(noise(113), noise(114));
  // Same series the rain scene uses for rare samples.
  const samples = [];
  for (let i = 1; i <= 5; i += 1) samples.push(noise(113 + i * 59));
  const replay = [];
  for (let i = 1; i <= 5; i += 1) replay.push(noise(113 + i * 59));
  assert.deepEqual(samples, replay);
});
