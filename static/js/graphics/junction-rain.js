/**
 * Junction splash-rain core, ported from the installed Junction extension.
 *
 * Source: resources/webview/render/animations.js, createMatrixLoader and its
 * randomSplashChar, assignSplashStyle, splashEntryY, resetSplashDrop, and
 * persistent branch of drawSplashLoader.
 *   animations.js SHA-256: 6b5bd745f863304880b30a04009243dc1656f00fd496afc5f9651a8e18ec5dfc
 *   animation-registry.js SHA-256: d29260acacdbc6eb3c6d7bcbe9666ace78d3078d01663d9682f448db898e28f8
 *
 * Glyphs travel at a constant speed in fixed lanes. Open Clank retains the
 * Junction spawn styling and its own rare upward drop.
 */

export const JUNCTION_RAIN_DEFAULTS = Object.freeze({
  quantity: 4, // Junction registry is 6.3; its source clamp makes 4 effective.
  speed: 1.6,
  flicker: .27,
  spread: 1,
  rainDown: true,
  reverseChance: 0,
  rareUpward: true,
  waves: false,
  charVariety: 1,
  minOpacity: .2,
  maxOpacity: 1,
  sizeVariance: .45,
  colorVariance: 0,
  emojiMix: false,
  emojiRarity: 10000000,
});

export const JUNCTION_KATAKANA = 'アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン';
export const JUNCTION_MATRIX = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@#$%^&*()_+-=[]{}|;:,.<>?/~`';
export const JUNCTION_RARE_UPWARD_PROBABILITY = 1 / 10000;

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, Number(value)));
}

function number(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function graphemes(value) {
  if (typeof Intl !== 'undefined' && Intl.Segmenter) {
    const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
    return Array.from(segmenter.segment(String(value || '')), item => item.segment);
  }
  return Array.from(String(value || ''));
}

function sourceControls(raw = {}) {
  const minOpacity = clamp(number(raw.minOpacity, JUNCTION_RAIN_DEFAULTS.minOpacity), .02, 1);
  return {
    quantity: clamp(number(raw.quantity, JUNCTION_RAIN_DEFAULTS.quantity), .3, 4),
    speed: clamp(number(raw.speed, JUNCTION_RAIN_DEFAULTS.speed), .1, 4),
    flicker: clamp(number(raw.flicker, JUNCTION_RAIN_DEFAULTS.flicker), 0, 1),
    spread: clamp(number(raw.spread, JUNCTION_RAIN_DEFAULTS.spread), 0, 4),
    rainDown: raw.rainDown == null ? JUNCTION_RAIN_DEFAULTS.rainDown : !!raw.rainDown,
    reverseChance: clamp(number(raw.reverseChance, JUNCTION_RAIN_DEFAULTS.reverseChance), 0, 100),
    rareUpward: raw.rareUpward == null ? JUNCTION_RAIN_DEFAULTS.rareUpward : !!raw.rareUpward,
    waves: !!raw.waves,
    charVariety: clamp(number(raw.charVariety, JUNCTION_RAIN_DEFAULTS.charVariety), .05, 1),
    minOpacity,
    maxOpacity: clamp(number(raw.maxOpacity, JUNCTION_RAIN_DEFAULTS.maxOpacity), minOpacity, 1),
    sizeVariance: clamp(number(raw.sizeVariance, JUNCTION_RAIN_DEFAULTS.sizeVariance), 0, 1.4),
    colorVariance: clamp(number(raw.colorVariance, JUNCTION_RAIN_DEFAULTS.colorVariance), 0, 1),
    emojiMix: !!raw.emojiMix,
    emojiRarity: Math.max(1, Math.round(number(raw.emojiRarity, JUNCTION_RAIN_DEFAULTS.emojiRarity))),
  };
}

function sourceCharset(mode, charset, emojiCharset) {
  if (mode === 'emoji') return graphemes(emojiCharset);
  if (charset === 'matrix') return graphemes(JUNCTION_MATRIX);
  return graphemes(JUNCTION_KATAKANA);
}

function sourceVariety(charset, charVariety) {
  const count = Math.max(1, Math.ceil(charset.length * charVariety));
  return charset.slice(0, count);
}

function directionForSpawn(controls, random) {
  let goesDown = controls.rainDown;
  if (random() < controls.reverseChance / 100) goesDown = !goesDown;
  // Open Clank host adaptation: Junction had no equivalent rare control.
  const rare = controls.rareUpward && random() < JUNCTION_RARE_UPWARD_PROBABILITY;
  if (rare) goesDown = !goesDown;
  return { goesDown, direction: goesDown ? 1 : -1, rare };
}

// Direct port of splashEntryY; only the name `canvas` becomes scene bounds.
function splashEntryY(scene, controls, goesDown, random) {
  const bandBase = controls.waves ? scene.height * .25 : scene.height;
  const band = bandBase * controls.spread * random();
  return goesDown
    ? (-scene.fontSize * (.5 + random() * .5) - band)
    : (scene.height + scene.fontSize * (.5 + random() * .5) + band);
}

// Direct port of randomSplashChar without Junction's VS Code message/setting side effect.
function randomSplashChar(scene, controls, random) {
  if (controls.emojiMix && scene.mode !== 'emoji' && random() < 1 / controls.emojiRarity) {
    return scene.emojiCharset[Math.floor(random() * scene.emojiCharset.length)] || scene.charset[0] || '◆';
  }
  return scene.variety[Math.floor(random() * scene.variety.length)] || scene.charset[0] || '◆';
}

function setSplashEmoji(drop, scene) {
  const emoji = scene.emojiIdentities.has(drop.ch);
  if (!emoji && drop.emoji) {
    drop.emojiAssetId = null;
    drop.themeEmojiAssetAssigned = false;
  }
  drop.emoji = emoji;
}

// Direct port of assignSplashStyle. Palette choice and RGB shift are consumed
// by the Open Clank adapter; the renderer always receives a hex colour plus alpha.
function assignSplashStyle(drop, scene, controls, random) {
  drop.scale = clamp(1 + ((random() * 2 - 1) * controls.sizeVariance), .45, 2.4);
  drop.drawSize = Math.max(2, Math.round(scene.fontSize * drop.scale));
  drop.behind = random() < .34;
  drop.alpha = controls.minOpacity + random() * (controls.maxOpacity - controls.minOpacity);
  if (drop.behind) drop.alpha *= .66;
  // Open Clank canon keeps its whole palette present. The source-equivalent
  // varyColor sample is separate so optional variation shifts that base only.
  drop.paletteVariant = random();
  drop.colorShift = random();
  // A Matrix drop can become a rare native emoji, so identity follows the
  // selected glyph on every restyle rather than just the scene mode.
  setSplashEmoji(drop, scene);
}

function resetSplashDrop(drop, scene, controls, random) {
  drop._achievementPainted = false;
  drop.x = drop.laneX + (random() - .5) * scene.fontSize * .12;
  const heading = directionForSpawn(controls, random);
  drop.direction = heading.direction;
  drop.rare = heading.rare;
  drop.y = splashEntryY(scene, controls, heading.goesDown, random);
  drop.rate = .55 + random() * .8;
  drop.ch = randomSplashChar(scene, controls, random);
  assignSplashStyle(drop, scene, controls, random);
}

function initialSplashDrop(index, scene, controls, random) {
  const laneX = (index + .5) * (scene.width / scene.columns);
  const heading = directionForSpawn(controls, random);
  const drop = {
    index,
    x: laneX,
    laneX,
    y: random() * scene.height,
    rate: .55 + random() * .8,
    direction: heading.direction,
    rare: heading.rare,
    ch: randomSplashChar(scene, controls, random),
    alpha: 1,
    behind: false,
    scale: 1,
    paletteVariant: 0,
    colorShift: .5,
    emoji: false,
  };
  assignSplashStyle(drop, scene, controls, random);
  return drop;
}

/**
 * Direct-port scene construction. Source `cols` becomes bounded `columns`;
 * both matrix and emoji follow the same viewport-font density formula.
 */
export function createJunctionRainScene({ width, height, size = 1, mode = 'matrix', controls: rawControls = {}, emojiCharset = '', random = Math.random } = {}) {
  const controls = sourceControls(rawControls);
  const safeWidth = Math.max(1, number(width, 1));
  const safeHeight = Math.max(1, number(height, 1));
  const fontSize = Math.max(3, Math.round(safeHeight * .014 * Math.max(.2, number(size, 1))));
  const charset = sourceCharset(mode, mode === 'emoji' ? 'emoji' : (rawControls.charsetPreset || 'katakana'), emojiCharset);
  const emoji = graphemes(emojiCharset);
  const scene = {
    mode,
    width: safeWidth,
    height: safeHeight,
    fontSize,
    columns: Math.min(640, Math.max(2, Math.floor((safeWidth / fontSize) * controls.quantity))),
    charset: charset.length ? charset : ['◆'],
    emojiCharset: emoji.length ? emoji : ['✨'],
    emojiIdentities: new Set(emoji.length ? emoji : ['✨']),
    variety: [],
    drops: [],
    controlSource: rawControls,
    controls,
    reducedFrameSeeded: false,
  };
  scene.variety = sourceVariety(scene.charset, controls.charVariety);
  scene.drops = Array.from({ length: scene.columns }, (_, index) => initialSplashDrop(index, scene, controls, random));
  return scene;
}

/** Advance each falling stream once per paint, independently of frame cadence. */
export function stepJunctionRain(scene, { controls: rawControls = scene.controls, deltaSeconds = 0, reduced = false, random = Math.random } = {}) {
  if (reduced) {
    if (!scene.reducedFrameSeeded) {
      scene.drops.forEach((drop, index) => {
        const column = (index + .5) / scene.columns;
        drop.x = scene.width * column;
        drop.y = scene.height * (.12 + random() * .76);
      });
      scene.reducedFrameSeeded = true;
    }
    return;
  }
  const elapsed = clamp(number(deltaSeconds, 0), 0, .05);
  if (!elapsed) return;
  if (rawControls !== scene.controlSource && rawControls !== scene.controls) {
    scene.controls = sourceControls(rawControls);
    scene.controlSource = rawControls;
  }
  const controls = scene.controls;
  const distance = elapsed * scene.fontSize * 18 * controls.speed;
  const flicker = scene.mode === 'emoji' ? 0 : 1 - (1 - controls.flicker) ** (elapsed * 60);
  for (const drop of scene.drops) {
    drop.y += drop.direction * drop.rate * distance;
    if (drop.direction > 0 ? drop.y > scene.height + drop.drawSize : drop.y < -drop.drawSize) {
      resetSplashDrop(drop, scene, controls, random);
    } else if (flicker && random() < flicker) {
      drop.ch = randomSplashChar(scene, controls, random);
      setSplashEmoji(drop, scene);
    }
  }
}
