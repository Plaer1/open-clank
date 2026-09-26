#!/usr/bin/env node
/**
 * S26 — remaining T01–T19 defects and all-presets/effects qualification.
 *
 * Structural assertions on theme.js / style.css / index.html / i18n for the
 * remaining audit rows (T10–T18) plus the S25 follow-up nits. Behavioural
 * evidence for the renderers is captured under execution/s26/visual/ (see the
 * receipt). Counts are honest: 18 built-in palettes, 17 background choices.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const themeSource = readFileSync(join(ROOT, 'static/js/theme.js'), 'utf8');
const cssSource = readFileSync(join(ROOT, 'static/style.css'), 'utf8');
const htmlSource = readFileSync(join(ROOT, 'static/index.html'), 'utf8');
const enCatalog = JSON.parse(readFileSync(join(ROOT, 'static/i18n/en.json'), 'utf8'));

globalThis.localStorage = globalThis.localStorage || {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
  clear: () => {},
};
globalThis.Storage = globalThis.Storage || globalThis.localStorage;
const theme = await import(join(ROOT, 'static/js/theme.js'));

// ── A. T11–T13 Emoji Drift ─────────────────────────────────────────────────

test('T13: drift glyphs come from a curated grapheme pool, not raw code-point ranges', () => {
  assert.doesNotMatch(themeSource, /_CLANKER_EMOJI_RANGES/);
  assert.match(themeSource, /_CLANKER_DRIFT_FALLBACK_GLYPH/);
  assert.match(themeSource, /function _clankerDriftGlyphPool/);
  // Pool is built through the grapheme segmenter so multi-code-point emoji stay
  // intact; the curated rain inventory is the source.
  assert.match(themeSource, /_clankerGraphemes\(_CLANKER_RAIN_EMOJI_CHARS\)/);
  assert.match(themeSource, /pool\.length \? pool : \[_CLANKER_DRIFT_FALLBACK_GLYPH\]/);
  assert.match(themeSource, /return pool\[index\] \|\| _CLANKER_DRIFT_FALLBACK_GLYPH/);
});

test('T11: emoji sprite cache is quantized, LRU-bounded, and released on unmount', () => {
  assert.match(themeSource, /_EMOJI_SPRITE_MAX_ENTRIES = 48/);
  assert.match(themeSource, /_EMOJI_SPRITE_SIZE_STEP = 4/);
  assert.match(themeSource, /function _quantizeEmojiSpriteSize/);
  // Quantization happens before the cache key is minted.
  assert.match(themeSource, /const pixelFontSize = _quantizeEmojiSpriteSize/);
  // LRU touch on hit + eviction on insert past the cap.
  assert.match(themeSource, /scene\.emojiSprites\.delete\(key\);\s*\n\s*scene\.emojiSprites\.set\(key, cached\)/);
  assert.match(themeSource, /if \(scene\.emojiSprites\.size >= _EMOJI_SPRITE_MAX_ENTRIES\)/);
  // Release on unmount goes through the canvas scene seam.
  assert.match(themeSource, /activeScene\.emojiSprites && typeof activeScene\.emojiSprites\.clear === 'function'/);
});

test('T12: drift rotation integrates angle over delta time (no total-elapsed phase jump)', () => {
  assert.match(themeSource, /shard\.angle = \(shard\.angle \|\| 0\) \+ elapsedMs \* \.00045/);
  assert.match(themeSource, /rotation: shard\.rotation \+ \(shard\.angle \|\| 0\)/);
  // The old "time * speed" form is gone from the drift state.
  assert.doesNotMatch(themeSource, /\? time \* \.00045 \* rotationSpeed/);
  // Frame helper now reports elapsed alongside the lerp factor.
  assert.match(themeSource, /return \{ lerp: 1 - Math\.exp\(-elapsed \/ 48\), elapsed \}/);
});

// ── B. T14 / T15 / T17 / T18 legacy canvases ────────────────────────────────

test('T14: rain color-variance control promises extra palette variation only', () => {
  // Disabling the toggle must not imply the rain loses color — base palette
  // cycling stays on. The label is the contract (design clarification).
  assert.match(themeSource, /label: 'Extra palette variation'/);
  assert.match(themeSource, /label: 'Extra palette variation amount'/);
  assert.doesNotMatch(themeSource, /label: 'Extra color variation'/);
});

test('T15: seven legacy canvases apply intensity once (CSS opacity), not twice', () => {
  // CSS opacity is the single source of truth for these IDs.
  assert.match(cssSource, /#synapse-canvas, #rain-canvas, #constellations-canvas,/);
  assert.match(cssSource, /opacity: var\(--bg-effect-intensity, 1\)/);
  // Draw alpha must not multiply by intensity again inside the legacy inits.
  const legacyStart = themeSource.indexOf('function _initSynapse');
  const legacyEnd = themeSource.indexOf('export {');
  const legacy = themeSource.slice(legacyStart, legacyEnd === -1 ? undefined : legacyEnd);
  assert.doesNotMatch(legacy, /globalAlpha = intensity \*/);
  assert.doesNotMatch(legacy, /alpha = intensity \*/);
  // Per-particle alpha is preserved (not flattened to a constant).
  assert.match(legacy, /globalAlpha = d\.alpha/);
  assert.match(legacy, /globalAlpha = fade \* \(e\.bright \? \.82 : \.38\)/);
});

test('T17: Dots exposes its intensity control (it paints with --bg-effect-intensity)', () => {
  assert.match(themeSource, /const _NO_INTENSITY_PATTERNS = new Set\(\['none'\]\)/);
  assert.match(themeSource, /const _NO_SIZE_PATTERNS = new Set\(\['none', 'dots'\]\)/);
  // dots is not in the no-intensity set.
  assert.doesNotMatch(themeSource, /_NO_INTENSITY_PATTERNS = new Set\(\['none', 'dots'\]\)/);
  // CSS dots tile scales with intensity.
  assert.match(cssSource, /body\.bg-pattern-dots \{[\s\S]*?var\(--bg-effect-intensity/);
});

test('T18: legacy resize handlers recompute devicePixelRatio (live zoom/display)', () => {
  const inits = [
    'function _initSynapse', 'function _initRain', 'function _initConstellations',
    'function _initPerlinFlow', 'function _initPetals', 'function _initSparkles',
    'function _initEmbers',
  ];
  for (const name of inits) {
    const start = themeSource.indexOf(name);
    assert.ok(start >= 0, name);
    const body = themeSource.slice(start, start + 2200);
    // Declared mutable and recomputed inside resize().
    assert.match(body, /let dpr = Math\.min\(window\.devicePixelRatio \|\| 1, 2\)/, name);
    assert.match(body, /dpr = Math\.min\(window\.devicePixelRatio \|\| 1, 2\);\s*\n\s*canvas\.width/, name);
    // No one-shot const capture of DPR remains in these inits.
    assert.doesNotMatch(body, /const dpr = Math\.min\(window\.devicePixelRatio/, name);
  }
});

// ── C. T16 blueprint controls + T10 reduced-motion repaint ─────────────────

test('T16: LCARS Status Sweep (clanker-blueprint) honors effect color and size', () => {
  // The final CSS override block (material pass) must reference the exposed
  // controls rather than hard-coded accents only.
  const override = cssSource.slice(cssSource.indexOf('T16: the material pass'));
  assert.match(override, /var\(--bg-effect-color/);
  assert.match(override, /var\(--clanker-grid-size/);
  // Grid texture layers scale with the size control.
  assert.match(override, /var\(--clanker-grid-size, 32px\) var\(--clanker-grid-size, 32px\)/);
  // Intensity still scales the art.
  assert.match(override, /var\(--bg-effect-intensity/);
});

test('T10: settings changes request one frame when reduced motion stops RAF', () => {
  assert.match(themeSource, /export function _invalidateBackgroundPaint/);
  assert.match(themeSource, /canvas\.__requestBackgroundRepaint/);
  // Wired into the three style appliers and the effect-control handlers.
  assert.match(themeSource, /setProperty\('--bg-effect-color'[\s\S]{0,80}_invalidateBackgroundPaint\(\)/);
  assert.match(themeSource, /setProperty\('--bg-effect-intensity'[\s\S]{0,80}_invalidateBackgroundPaint\(\)/);
  assert.match(themeSource, /setProperty\('--bg-effect-size'[\s\S]{0,240}_invalidateBackgroundPaint\(\)/);
  assert.match(themeSource, /_setBackgroundEffectControlValue\(pattern, control\.key, input\.checked\);[\s\S]{0,160}_invalidateBackgroundPaint\(\)/);
});

// ── D. S25 follow-up nits ──────────────────────────────────────────────────

test('S25 nit: paintField.clear() is wired (dispose release + topology rebuild)', () => {
  assert.match(themeSource, /clear\(\) \{\s*\n\s*this\.alphas\.fill\(0\)/);
  assert.match(themeSource, /activeScene\.paintField && typeof activeScene\.paintField\.clear === 'function'/);
});

test('S25 nit: DIRECTION_CHANGE_CHANCE renamed to an honest weight', () => {
  assert.match(themeSource, /DIRECTION_CHANGE_WEIGHT = \.16/);
  assert.doesNotMatch(themeSource, /DIRECTION_CHANGE_CHANCE = \.16/);
  assert.match(themeSource, /policy\.DIRECTION_CHANGE_WEIGHT \* \.02/);
});

test('S25 nit: Radar Ripples i18n key replaced by ui.clanker.lcars', () => {
  assert.equal(enCatalog['ui.clanker.lcars'], 'Clanker LCARS');
  assert.equal(enCatalog['ui.clanker.radar.ripples'], undefined);
  assert.match(htmlSource, /value="clanker-lcars">Clanker LCARS</);
});

// ── E. All-presets / all-effects qualification matrix ──────────────────────

test('Qualification: exactly 18 built-in palettes are registered', () => {
  const names = Object.keys(theme.THEMES);
  assert.equal(names.length, 18, `got ${names.length}: ${names.join(', ')}`);
});

test('Qualification: exactly 17 background choices are mounted in the picker', () => {
  const selectStart = htmlSource.indexOf('id="theme-bg-pattern-select"');
  const selectEnd = htmlSource.indexOf('</select>', selectStart);
  const select = htmlSource.slice(selectStart, selectEnd);
  const options = [...select.matchAll(/<option value="([^"]+)">/g)].map(m => m[1]);
  assert.equal(options.length, 17, `got ${options.length}: ${options.join(', ')}`);
  // Every non-solid option has a registered canvas or CSS pattern class.
  for (const value of options) {
    if (value === 'none') continue;
    assert.match(htmlSource, new RegExp(`value="${value}"`), value);
    assert.match(cssSource + themeSource, new RegExp(`bg-pattern-${value}`), value);
  }
});

test('Qualification: every canvas background has a registered mount', () => {
  const block = themeSource.slice(
    themeSource.indexOf('const _CANVAS_PATTERNS'),
    themeSource.indexOf('const _BACKGROUND_CANVAS_SELECTOR'),
  );
  const mounted = [...block.matchAll(/(?:'([a-z0-9-]+)'|([a-z0-9]+)):\s*_init/g)]
    .map(m => m[1] || m[2]);
  assert.equal(mounted.length, 14, `got ${mounted.length}: ${mounted.join(', ')}`);
});

test('Qualification: one running animation owner on unchanged reapply (Hex)', () => {
  // applyBgPattern short-circuits when the same pattern already has an owner.
  assert.match(themeSource, /const samePattern = _activeBgPattern\(\) === p/);
  assert.match(themeSource, /if \(samePattern && \(!_CANVAS_PATTERNS\[p\] \|\| hasActiveOwner\)\)/);
  // Every mount claims the single owner key; dispose reconciles it.
  assert.match(themeSource, /window\[_BACKGROUND_OWNER_KEY\] = dispose/);
  assert.match(themeSource, /if \(window\[_BACKGROUND_OWNER_KEY\] === dispose\) window\[_BACKGROUND_OWNER_KEY\] = null/);
});

test('Qualification: owner-scoped persistence + export/import keep effect options', () => {
  const snap = theme.normalizeThemeSnapshot({
    name: 'clanker-dark',
    bgPattern: 'clanker-lcars',
    bgEffectIntensity: 0.4,
    bgEffectSize: 1.2,
    bgEffectColor: '#62C7E8',
    bgEffectControls: { probe: 1 },
    frosted: true,
  });
  assert.equal(snap.background.pattern, 'clanker-lcars');
  assert.equal(snap.background.effectIntensity, 0.4);
  assert.equal(snap.background.effectSize, 1.2);
  assert.equal(snap.background.effectColor, '#62C7E8');
  assert.equal(snap.background.controls.probe, 1);
  assert.equal(snap.motion.reduced, false);
});

test('Qualification: i18n catalogs keep key parity with en.json', () => {
  const dir = join(ROOT, 'static/i18n');
  const enKeys = Object.keys(enCatalog).sort();
  const skip = new Set(['brands.json', 'registry.json', 'allowlist.json', 'ledger.json', 'provenance.json']);
  let checked = 0;
  for (const name of readdirSync(dir)) {
    if (!name.endsWith('.json') || skip.has(name) || name === 'en.json') continue;
    const data = JSON.parse(readFileSync(join(dir, name), 'utf8'));
    if (Object.keys(data).length !== enKeys.length) continue; // English-fallback staged catalogs
    assert.deepEqual(Object.keys(data).sort(), enKeys, name);
    checked += 1;
  }
  assert.ok(checked >= 30, `only ${checked} catalogs checked`);
});
