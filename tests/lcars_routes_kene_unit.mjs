#!/usr/bin/env node
/**
 * S25 — LCARS redesign, Signal Routes beads, Kene grid paint.
 *
 * Mix of real module behaviour (normalizeThemeSnapshot migration) and
 * source-structure assertions on theme.js where the logic is a browser
 * closure. Visual/behavioural evidence for the renderers is captured by the
 * fixture harness under execution/s25/visual/ (see receipt).
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const themeSource = readFileSync(join(ROOT, 'static/js/theme.js'), 'utf8');
const htmlSource = readFileSync(join(ROOT, 'static/index.html'), 'utf8');

// theme.js expects browser-ish globals at import time; stub the minimum.
globalThis.localStorage = globalThis.localStorage || {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
  clear: () => {},
};
globalThis.Storage = globalThis.Storage || globalThis.localStorage;
const theme = await import(join(ROOT, 'static/js/theme.js'));

// ── A. LCARS migration, composition and reduced motion ─────────────────────

test('LCARS: Radar pattern ID migrates to clanker-lcars and preserves settings', () => {
  const migrated = theme.normalizeThemeSnapshot({
    name: 'clanker-dark',
    bgPattern: 'clanker-radar',
    bgEffectIntensity: 0.42,
    bgEffectSize: 1.35,
    bgEffectColor: '#62C7E8',
    bgEffectControls: { probeKey: 7 },
  });
  assert.equal(migrated.background.pattern, 'clanker-lcars');
  assert.equal(migrated.bgPattern, 'clanker-lcars');
  // Saved intensity/size/color/controls ride along with the migration.
  assert.equal(migrated.background.effectIntensity, 0.42);
  assert.equal(migrated.background.effectSize, 1.35);
  assert.equal(migrated.background.effectColor, '#62C7E8');
  assert.equal(migrated.background.controls.probeKey, 7);

  // Already-migrated IDs are stable (no double alias).
  const stable = theme.normalizeThemeSnapshot({
    name: 'clanker-dark',
    bgPattern: 'clanker-lcars',
    bgEffectIntensity: 0.42,
  });
  assert.equal(stable.background.pattern, 'clanker-lcars');
  assert.equal(stable.background.effectIntensity, 0.42);
});

test('LCARS: pattern is registered and the old radar ID is gone from the maps', () => {
  assert.match(themeSource, /'clanker-lcars':\s*_initClankerLcars/);
  assert.doesNotMatch(themeSource, /'clanker-radar':\s*_initClankerRadar/);
  assert.match(themeSource, /'clanker-radar':\s*'clanker-lcars'/);
  assert.match(themeSource, /bg-pattern-clanker-lcars/);
  assert.doesNotMatch(themeSource, /bg-pattern-clanker-radar/);
  assert.match(themeSource, /function _initClankerLcars\(/);
  assert.doesNotMatch(themeSource, /function _initClankerRadar\(/);
  // HTML label migrated.
  assert.match(htmlSource, /value="clanker-lcars">Clanker LCARS</);
  assert.doesNotMatch(htmlSource, /clanker-radar/);
  // The distinct LCARS Status Sweep background is preserved untouched.
  assert.match(htmlSource, /value="clanker-blueprint">Clanker LCARS Status Sweep</);
  assert.match(themeSource, /'clanker-blueprint'/);
});

test('LCARS: composition is elbows/bars/tiles — no concentric rings or centers', () => {
  // The old Radar scene was a list of concentric `centers` with rings/ticks/
  // blips. The LCARS scene is elbows + segmented bars + status tiles + scans.
  assert.match(themeSource, /const elbows = \[/);
  assert.match(themeSource, /const makeBar = /);
  assert.match(themeSource, /status tiles/i);
  assert.match(themeSource, /stepped scans/i);
  // No expanding concentric rings.
  assert.doesNotMatch(themeSource, /for \(let ring = 1; ring <= 5/);
  assert.doesNotMatch(themeSource, /centers:\s*specs\.map/);
  // Center-open composition is an explicit build concern.
  assert.match(themeSource, /Center stays open for readable applets/);
  // Theme-relative accents plus low-alpha secondary detail.
  assert.match(themeSource, /low-alpha secondary cyan\/lilac/i);
});

test('LCARS: reduced motion is composed static art, not an empty canvas', () => {
  // The draw path pins renderTime to 0 under reduced motion and still paints
  // the full composition (rails, bars, tiles, resting scans).
  const lcars = themeSource.slice(
    themeSource.indexOf('function _initClankerLcars'),
    themeSource.indexOf('function _clankerNoise'),
  );
  assert.match(lcars, /const renderTime = reduced \? 0 : time/);
  // Pulses are time-locked; tiles breathe only when motion is allowed.
  assert.match(lcars, /reduced \? 1 : 1 \+ Math\.sin/);
  assert.match(lcars, /const step = reduced/);
  // roundRect has an ordinary fallback for engines without it.
  assert.match(lcars, /typeof ctx\.roundRect === 'function'/);
});

test('LCARS: size/intensity/color controls are read live from the theme config', () => {
  const lcars = themeSource.slice(
    themeSource.indexOf('function _initClankerLcars'),
    themeSource.indexOf('function _clankerNoise'),
  );
  // Geometry scales with size; alpha scales with intensity; hues come from
  // the palette (effect color + gold/lilac slots).
  assert.match(lcars, /size\b/);
  assert.match(lcars, /intensity\b/);
  assert.match(lcars, /colors\[0\]/);
  assert.match(lcars, /colors\[1\]/);
});

// ── B. Signal Routes — persistent beads, no teleports ───────────────────────

test('Signal Routes: bead movement is distance-integrated, not modulo-wrapped', () => {
  const routes = themeSource.slice(
    themeSource.indexOf('function _initClankerRoutefield'),
    themeSource.indexOf('const SNAKE_FADE_OUT_MS'),
  );
  // The old `% 1` progress wrap is gone from the bead path.
  assert.doesNotMatch(routes, /const progress = \(renderTime \/ \(\d+/);
  assert.doesNotMatch(routes, /\+ route\.phase\) % 1/);
  // Persistent bead state on the graph.
  assert.match(routes, /MAX_ROUTE_BEADS/);
  assert.match(routes, /beads\.push\(/);
  assert.match(routes, /bead\.distance \+=/);
  // Continuous handoff at junctions: the bead enters the next edge at the
  // shared node (distance 0 or total), never jumping across the map.
  assert.match(routes, /bead\.distance = enterFromB \? next\.samples\.total : 0/);
  assert.match(routes, /Continuous handoff/);
});

test('Signal Routes: pool is capped, branches are cooled down, no snake bodies', () => {
  const routes = themeSource.slice(
    themeSource.indexOf('function _initClankerRoutefield'),
    themeSource.indexOf('const SNAKE_FADE_OUT_MS'),
  );
  assert.match(routes, /const MAX_ROUTE_BEADS = 48/);
  assert.match(routes, /scene\.beads\.length > policy\.MAX_ROUTE_BEADS/);
  assert.match(routes, /BRANCH_COOLDOWN_MS/);
  assert.match(routes, /arrivingAt\.lastBranchAt/);
  // Retirement keeps density stable over long runs.
  assert.match(routes, /BEAD_LIFETIME_MS/);
  assert.match(routes, /scene\.stats\.retirements/);
  // Beads are point glows: no tail array, no snake step ladder.
  assert.doesNotMatch(routes, /tailSteps/);
  assert.doesNotMatch(routes, /for \(let step = tailSteps/);
  assert.match(routes, /Short local glow only/);
  // Elapsed-time integration, not a global phase.
  assert.match(routes, /Math\.min\(64, renderTime - scene\.lastTime\)/);
});

test('Signal Routes: style-only updates do not rebuild the scene (positions persist)', () => {
  // `_mountClankerEffect` scene key must exclude palette/intensity (T08).
  const mount = themeSource.slice(
    themeSource.indexOf('function _mountClankerEffect'),
    themeSource.indexOf('function _initClankerRoutefield'),
  );
  assert.doesNotMatch(mount, /sceneStyleKey/);
  assert.match(mount, /const nextSceneKey = `\$\{width\}:\$\{height\}:\$\{config\.size\}:\$\{safeBounds\.inset\}:\$\{controlSceneKey\}`/);
});

// ── C. Kene grid paint ─────────────────────────────────────────────────────

test('Kene: snakePaintTrail wires a bounded grid paint field separate from bodies', () => {
  const kene = themeSource.slice(
    themeSource.indexOf('function _initClankerKeneWeave'),
    themeSource.indexOf('function _initClankerLcars'),
  );
  assert.match(kene, /paintField/);
  assert.match(kene, /deposit\(x, y, colorIndex, amount\)/);
  assert.match(kene, /decay\(elapsedMs\)/);
  assert.match(kene, /clear\(\)/);
  // Bounded: the grid is clamped, not free-growing.
  assert.match(kene, /Math\.min\(96, Math\.ceil\(width \/ paintCell\)\)/);
  assert.match(kene, /Math\.min\(96, Math\.ceil\(height \/ paintCell\)\)/);
  // Off stops new paint; existing paint keeps fading.
  assert.match(kene, /const paintTrailOn = getBackgroundEffectControlValue\('clanker-kene-weave', 'snakePaintTrail', false\)/);
  assert.match(kene, /if \(paintTrailOn && paintField\)/);
  // The registered control exists with the expected label.
  assert.match(themeSource, /key: 'snakePaintTrail', label: 'Paint trail on grid'/);
});

test('Kene: paint is drawn as grid cells, not as snake body strokes', () => {
  const kene = themeSource.slice(
    themeSource.indexOf('function _initClankerKeneWeave'),
    themeSource.indexOf('function _initClankerLcars'),
  );
  assert.match(kene, /ctx\.fillRect\(column \* cellW, row \* cellH/);
  // Deposits come from head/tail samples and are stored on the field.
  assert.match(kene, /paintField\.deposit\(headPoint\.x, headPoint\.y/);
  // The paint layer sits under the snakes (drawn before the snake loop).
  const paintDrawIndex = kene.indexOf('Paint is drawn under the snakes');
  const snakeLoopIndex = kene.indexOf('for (let signalIndex = 0;');
  assert(paintDrawIndex >= 0 && snakeLoopIndex > paintDrawIndex,
    'paint layer must draw before the snake loop');
});

test('Kene: topology reset clears paint; style/toggle updates do not restart trajectories', () => {
  const kene = themeSource.slice(
    themeSource.indexOf('function _initClankerKeneWeave'),
    themeSource.indexOf('function _initClankerLcars'),
  );
  // A fresh scene (topology reset) constructs an empty field.
  assert.match(kene, /colors: new Uint8Array\(paintCols \* paintRows\)/);
  assert.match(kene, /alphas: new Float32Array\(paintCols \* paintRows\)/);
  // Style-only updates repaint the static raster without touching snakes.
  assert.match(kene, /scene\.rasterizeStatic\(colors, outline, intensity, size\)/);
  assert.match(kene, /Snake state is\n?\s*untouched|trajectories continue/i);
  // Snake dynamics and seeded performance controls are preserved.
  assert.match(kene, /snakeCount/);
  assert.match(kene, /snakeSpeed/);
  assert.match(kene, /snakeLifetimeVariation/);
  assert.match(kene, /shorterLastLonger/);
  assert.match(kene, /longerDisappearSooner/);
});

// ── S11–S24 preservation smoke ─────────────────────────────────────────────

test('S11–S24 preserved: rain, rare inversion, graphics substrate, gold accents intact', () => {
  // S24 rare inversion still present and default-on in the theme controls.
  assert.match(themeSource, /key: 'splashRareUpward'.*default: true/);
  const rainPhysics = readFileSync(join(ROOT, 'static/js/graphics/rain-physics.js'), 'utf8');
  assert.match(rainPhysics, /RARE_UPWARD_PROBABILITY = 1 \/ 10000/);
  // S23 graphics substrate still wired for the rain consumer.
  assert.match(themeSource, /createGraphicsConsumer/);
  assert.match(themeSource, /createObstacleField/);
  // S22 gold accents.
  assert.match(themeSource, /THEME_ACCENT_DEFAULT = '#F6BE48'/);
  assert.match(themeSource, /'#B83B78'/); // kept in LEGACY_CLANKER_ACCENTS
  // ONE scene owner key is still the single owner seam.
  assert.match(themeSource, /__openClankBackgroundOwner/);
});
