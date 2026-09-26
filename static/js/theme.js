// Theme system — preset themes + custom color editing, stored in localStorage
// ES6 module

import Storage from './storage.js';
import uiModule from './ui.js';
import { initColorPickers, attachColorPicker } from './colorPicker.js';
import { hexToRgb } from './color/hex.js';
import { makeWindowDraggable } from './windowDrag.js';
import { snapModalToZone } from './tileManager.js';
import { createGraphicsConsumer } from './graphics/consumer.js';
import { createObstacleField, PAINTED_ALPHA_THRESHOLD } from './graphics/obstacle-field.js';
import {
  composeRainDirection,
  resolveCollision,
  spawnPosition,
} from './graphics/rain-physics.js';

export const THEMES = {
  'clanker-dark': {
    bg:'#191A1E', fg:'#FFF4D6', panel:'#25272C', border:'#555A62', red:'#F6BE48',
    advanced: { userBubbleBg:'#30333A', aiBubbleBg:'#25272C', bubbleBorder:'#555A62',
                sidebarBg:'#202227', brandColor:'#F6BE48', brandMixTo:'#ED6AB0',
                hamburgerColor:'#FFF4D6', inputBg:'#2B2E34', inputBorder:'#555A62',
                sendBtnBg:'#F6BE48', sendBtnHover:'#C4922A', codeBg:'#101114',
                codeFg:'#FFF4D6', toggleActive:'#A8DE53' },
  },
  'clanker-light': {
    bg:'#F3EEDB', fg:'#17202A', panel:'#FFF9E7', border:'#26323D', red:'#F4B827',
    advanced: { userBubbleBg:'#DCEAF0', aiBubbleBg:'#FFF9E7', bubbleBorder:'#26323D',
                sidebarBg:'#F4B827', brandColor:'#EC635C', brandMixTo:'#D94B9C',
                hamburgerColor:'#17202A', inputBg:'#FFF9E7', inputBorder:'#26323D',
                sendBtnBg:'#F4B827', sendBtnHover:'#C4922A', codeBg:'#111A27',
                codeFg:'#FFF9E7', toggleActive:'#4F9F38' },
  },
  dark:       { bg:'#282c34', fg:'#9cdef2', panel:'#111111', border:'#355a66', red:'#e06c75' },
  light:      { bg:'#f0ebe3', fg:'#5a5248', panel:'#faf6f0', border:'#d4cdc2', red:'#c47d5a' },
  midnight:   { bg:'#0d1117', fg:'#c9d1d9', panel:'#161b22', border:'#30363d', red:'#f85149' },
  paper:      { bg:'#faf8f5', fg:'#3b3836', panel:'#ffffff', border:'#d5d0c8', red:'#c5ac4a' },
  // Spicy / fun themes
  cyberpunk:  { bg:'#0a0a0f', fg:'#0ff0fc', panel:'#12101a', border:'#9b30ff', red:'#e040fb' },
  retrowave:  { bg:'#1a1a2e', fg:'#e94560', panel:'#16213e', border:'#533483', red:'#e94560' },
  forest:     { bg:'#1b2a1b', fg:'#a8d5a2', panel:'#142414', border:'#3d6b3d', red:'#7cb871' },
  ocean:      { bg:'#0b1a2c', fg:'#64d2ff', panel:'#091422', border:'#1e5074', red:'#4facfe' },
  ume:        { bg:'#2b1b2e', fg:'#f5c2e7', panel:'#1e1420', border:'#6c4675', red:'#f5a0c0' },
  copper:     { bg:'#1c1410', fg:'#e8c39e', panel:'#140f0a', border:'#7a5533', red:'#d4764e' },
  terminal:   { bg:'#000000', fg:'#00ff41', panel:'#0a0a0a', border:'#003b00', red:'#00ff41' },
  organs:     { bg:'#0a0406', fg:'#efe1c8', panel:'#15080a', border:'#3a1519', red:'#c83240' },
  lavender:   { bg:'#f3eef8', fg:'#3d3551', panel:'#faf7ff', border:'#cec3de', red:'#9b6dcc' },
  gpt:        { bg:'#212121', fg:'#ececec', panel:'#171717', border:'#424242', red:'#949494',
                advanced: { sendBtnBg: '#949494', sendBtnHover: '#7f7f7f',
                            userBubbleBg: '#2f2f2f', aiBubbleBg: '#171717',
                            inputBg: '#2f2f2f', brandColor: '#ffffff', brandMixTo: '#ffffff' } },
  claude:     { bg:'#262624', fg:'#f5f4f0', panel:'#30302e', border:'#4a4a47', red:'#c6613f' },
  cute:       { bg:'#fff0f5', fg:'#d4608a', panel:'#fff8fa', border:'#f0c0d0', red:'#ff6b9d' },
};

const THEME_LABELS = {
  'clanker-dark': 'Clanker Dark',
  'clanker-light': 'Clanker Light',
  dark: 'original',
  gpt: 'GPT',
};

const DEFAULT_THEME = 'clanker-dark';
const LS_KEY = 'odysseus-theme';
const CUSTOM_THEMES_KEY = 'odysseus-custom-themes';
const THEME_SNAPSHOT_VERSION = 2;
const AUTH_USER_KEY = 'odysseus-auth-user';
const THEME_OWNER_PREFIX = `${LS_KEY}:scope:`;
const LEGACY_CLANKER_ACCENTS = new Set(['#5A9EF5', '#2469D8', '#B83B78']);
const THEME_ACCENT_DEFAULT = '#F6BE48';
const THEME_ACCENT_HOVER = '#C4922A';
const THEME_ACCENT_FOCUS = '#FFD97A';
const THEME_ACCENT_DISABLED = '#C4A46A';
const THEME_ERROR_DEFAULT = '#FF776E';
const THEME_SUCCESS_DEFAULT = '#8DD85D';
let _themeOwner = null;
let _themeHydrationGeneration = 0;
let _themeHydrationPromise = null;
let _themeHydrationRequest = null;
let _themeInitPromise = null;

const FONT_MAP = {
  'liga-comic-mono': "'Liga Comic Mono', 'Fira Code', monospace",
  mono: "'Fira Code', monospace",
  sans: "system-ui, -apple-system, 'Segoe UI', sans-serif",
  serif: "Georgia, 'Times New Roman', serif",
  opendyslexic: "'OpenDyslexic', sans-serif",
};
const DEFAULT_FONT = 'mono';
const DEFAULT_DENSITY = 'comfortable';
const MAX_CUSTOM_THEMES = 8;

// Default background patterns for built-in themes
const THEME_DEFAULT_PATTERN = {
  'clanker-dark':  'clanker-routefield',
  'clanker-light': 'clanker-blueprint',
  dark:       'none',
  light:      'dots',
  midnight:   'rain',
  paper:      'dots',
  cyberpunk:  'synapse',
  retrowave:  'embers',
  forest:     'petals',
  ocean:      'constellations',
  terminal:   'perlin-flow',
  organs:     'rain',
  ume:        'petals',
  cute:       'sparkles',
};

// Default effect colors for specific themes (overrides --fg)
const THEME_DEFAULT_EFFECT_COLOR = {
  'clanker-dark':  '#62C7E8',
  'clanker-light': '#2469D8',
  midnight:   '#ffffff',
  organs:     '#451616',
  cute:       '#ff8cb8',
  ume:        '#f5a0c0',
};

// Default effect intensity (0..1) per theme. Any theme not listed defaults to 1.
const THEME_DEFAULT_INTENSITY = {
  'clanker-dark':  0.64,
  'clanker-light': 0.55,
  midnight:   0.5,
  terminal:   0.8,
  organs:     0.65,
};

const THEME_DEFAULT_SIZE = {
  'clanker-dark': 1,
  'clanker-light': 1,
};

const THEME_DEFAULT_FONT = {
  'clanker-dark': 'liga-comic-mono',
  'clanker-light': 'liga-comic-mono',
};

// Default frosted-glass state per theme. Themes not listed default to false.
const THEME_DEFAULT_FROSTED = {
  lavender:   true,
};

function _isPlainObject(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function _copyThemeValue(value) {
  if (Array.isArray(value)) return value.map(_copyThemeValue);
  if (!_isPlainObject(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, _copyThemeValue(item)]));
}

function _freezeThemeValue(value) {
  if (!_isPlainObject(value) && !Array.isArray(value)) return value;
  Object.values(value).forEach(_freezeThemeValue);
  return Object.freeze(value);
}

function _themeColor(value) {
  if (typeof value !== 'string') return null;
  const color = value.trim();
  return /^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i.test(color) ? color : null;
}

function _themeOwnerKey(owner = _themeOwner) {
  const id = String(owner || '').trim();
  return id ? `${THEME_OWNER_PREFIX}${encodeURIComponent(id)}` : LS_KEY;
}

function _customThemeStorageKey(owner = _themeOwner) {
  return owner ? `${CUSTOM_THEMES_KEY}:scope:${encodeURIComponent(String(owner))}` : CUSTOM_THEMES_KEY;
}

function _themeSourcePalette(input) {
  if (!_isPlainObject(input)) return {};
  const nested = _isPlainObject(input.palette) ? input.palette : {};
  const flat = _isPlainObject(input.colors) ? input.colors : {};
  const overrides = _isPlainObject(input.overrides) && _isPlainObject(input.overrides.colors)
    ? input.overrides.colors : {};
  return { ...flat, ...nested, ...overrides };
}

function _isLegacyClankerDefault(name, source, base, context = {}) {
  // A caller saving a freshly edited palette has provenance that a legacy
  // localStorage record lacks. Preserve an intentional blue value in that
  // case; only unversioned reads should apply the conservative migration.
  if (context.preserveExplicit) return false;
  if (context.version >= THEME_SNAPSHOT_VERSION && context.overridePresent) return false;
  if (!['clanker-dark', 'clanker-light'].includes(name)) return false;
  const oldAccent = LEGACY_CLANKER_ACCENTS.has(String(source.red || '').toUpperCase());
  return oldAccent
    && ['bg', 'fg', 'panel', 'border'].every(key => !source[key] || String(source[key]).toUpperCase() === String(base[key]).toUpperCase());
}

/**
 * Return the complete versioned theme state used by boot, preview, hydration,
 * save and reset. Flat aliases are retained for older callers while the
 * nested fields make provenance and absent values explicit.
 */
export function normalizeThemeSnapshot(input = null, context = {}) {
  const source = _isPlainObject(input) ? input : {};
  let name = source.identity?.name || source.name || context.name || DEFAULT_THEME;
  if (name === 'chatgpt') name = 'gpt';
  if (name === 'sakura') name = 'ume';
  const builtIn = Object.prototype.hasOwnProperty.call(THEMES, name);
  if (!builtIn && !_isPlainObject(source.colors) && !_isPlainObject(source.palette)
      && !_isPlainObject(source.overrides?.colors)) return null;

  const base = builtIn ? THEMES[name] : (THEMES[DEFAULT_THEME] || {});
  const rawPalette = _themeSourcePalette(source);
  const legacyDefault = _isLegacyClankerDefault(name, rawPalette, base, {
    ...context,
    version: source.version,
    overridePresent: source.identity?.overridePresent,
  });
  const palette = {};
  for (const key of ['bg', 'fg', 'panel', 'border', 'red']) {
    const candidate = _themeColor(rawPalette[key]);
    const fallback = _themeColor(base[key]) || (key === 'red' ? THEME_ACCENT_DEFAULT : '#000000');
    palette[key] = (legacyDefault && key === 'red') ? (_themeColor(base.red) || THEME_ACCENT_DEFAULT) : (candidate || fallback);
  }

  const rawAdvanced = {
    ...(builtIn && _isPlainObject(base.advanced) ? _copyThemeValue(base.advanced) : {}),
    ...(_isPlainObject(rawPalette.advanced) ? _copyThemeValue(rawPalette.advanced) : {}),
    ...(_isPlainObject(source.advanced) ? _copyThemeValue(source.advanced) : {}),
    ...(_isPlainObject(source.overrides?.advanced) ? _copyThemeValue(source.overrides.advanced) : {}),
  };
  if (legacyDefault) {
    const defaults = base.advanced || {};
    for (const key of Object.keys(defaults)) rawAdvanced[key] = defaults[key];
  }
  const explicitAdvanced = {};
  for (const [key, value] of Object.entries(rawAdvanced)) {
    const color = _themeColor(value);
    explicitAdvanced[key] = color || value;
  }
  const clankerDefault = name === 'clanker-dark' || name === 'clanker-light';
  const accent = _themeColor(explicitAdvanced.accentPrimary) || palette.red;
  const accentHover = _themeColor(explicitAdvanced.accentHover)
    || (clankerDefault ? THEME_ACCENT_HOVER : (_themeColor(explicitAdvanced.sendBtnHover) || palette.red));
  const accentFocus = _themeColor(explicitAdvanced.accentFocus)
    || (clankerDefault ? THEME_ACCENT_FOCUS : palette.red);
  const accentDisabled = _themeColor(explicitAdvanced.accentDisabled)
    || (clankerDefault ? THEME_ACCENT_DISABLED : palette.red);
  const accentError = _themeColor(explicitAdvanced.accentError) || THEME_ERROR_DEFAULT;
  const normalizedAdvanced = {
    ...explicitAdvanced, accentPrimary: accent, accentHover, accentFocus,
    accentDisabled, accentError,
  };
  // Read the nested snapshot fields first, then fill gaps from the legacy
  // flat record.  A partially migrated record can contain both; choosing the
  // nested object wholesale used to discard its legacy pattern/options.
  const options = {
    ...(source.background || {}),
    ...source,
    ...(_isPlainObject(source.background) ? source.background : {}),
  };
  const typography = {
    ...source,
    ...(_isPlainObject(source.typography) ? source.typography : {}),
  };
  // Normalize legacy pattern aliases at every load/import/save path (T19).
  // `clanker-radar` (Radar Ripples) migrates to the S25 LCARS redesign while
  // keeping saved intensity/size/color preferences on the same theme record.
  const PATTERN_ALIASES = { 'clanker-sweep': null, 'clanker-radar': 'clanker-lcars' };
  let rawPattern = options.pattern || options.bgPattern || THEME_DEFAULT_PATTERN[name] || 'none';
  if (Object.prototype.hasOwnProperty.call(PATTERN_ALIASES, rawPattern)) {
    rawPattern = PATTERN_ALIASES[rawPattern] || THEME_DEFAULT_PATTERN[name] || 'none';
  }
  const pattern = rawPattern;
  const effectColor = options.effectColor || options.bgEffectColor || THEME_DEFAULT_EFFECT_COLOR[name] || '';
  const effectIntensity = options.effectIntensity ?? options.bgEffectIntensity
    ?? THEME_DEFAULT_INTENSITY[name] ?? 1;
  const effectSize = options.effectSize ?? options.bgEffectSize ?? THEME_DEFAULT_SIZE[name] ?? 1;
  const controls = options.controls || options.bgEffectControls || {};
  const font = THEME_DEFAULT_FONT[name] || typography.font || DEFAULT_FONT;
  const density = typography.density || DEFAULT_DENSITY;
  const frosted = options.frosted !== undefined ? !!options.frosted : THEME_DEFAULT_FROSTED[name] === true;
  const reduced = source.motion?.reduced !== undefined
    ? !!source.motion.reduced : !!source.reducedMotion;
  const paletteOverrides = builtIn
    ? Object.fromEntries(Object.keys(rawPalette).filter(key => ['bg', 'fg', 'panel', 'border', 'red'].includes(key)
      && !legacyDefault && _themeColor(rawPalette[key]) && String(rawPalette[key]).toUpperCase() !== String(base[key]).toUpperCase())
      .map(key => [key, palette[key]]))
    : { ...palette };
  const snapshot = {
    version: THEME_SNAPSHOT_VERSION,
    accountId: context.accountId || source.accountId || null,
    identity: { name, kind: builtIn ? 'builtin' : 'custom', overridePresent: Object.keys(paletteOverrides).length > 0 },
    palette,
    overrides: { colors: paletteOverrides, advanced: normalizedAdvanced },
    typography: { font, density },
    background: {
      pattern, effectColor, effectIntensity: Number(effectIntensity), effectSize: Number(effectSize),
      controls: _copyThemeValue(controls), frosted,
    },
    motion: { reduced },
    // Compatibility aliases for the existing theme UI and integrations.
    name, colors: { ...palette, advanced: { ...normalizedAdvanced } }, font, density,
    bgPattern: pattern, bgEffectColor: effectColor, bgEffectIntensity: Number(effectIntensity),
    bgEffectSize: Number(effectSize), bgEffectControls: _copyThemeValue(controls), frosted,
  };
  // The controller never mutates a snapshot. A new value is produced on each
  // preview/save, so an old account response cannot alter live state.
  return _freezeThemeValue(snapshot);
}

export function themeStorageKey(owner = null) {
  return _themeOwnerKey(owner);
}

// ── Custom theme persistence ──
function _loadCustomThemes() {
  const value = Storage.getJSON(_customThemeStorageKey(), {});
  return _isPlainObject(value) ? value : {};
}
function _saveCustomThemes(obj) {
  Storage.setJSON(_customThemeStorageKey(), obj);
}
function _writeCustomThemeEntry(name, colors, opts) {
  const ct = _loadCustomThemes();
  // Enforce limit — allow overwriting existing, block new past max
  if (!ct[name] && Object.keys(ct).length >= MAX_CUSTOM_THEMES) {
    return 'limit';
  }
  const snapshot = normalizeThemeSnapshot({ name, colors, ...(opts || {}) }, { name, accountId: _themeOwner, preserveExplicit: true });
  if (!snapshot) return 'invalid';
  const entry = { ...snapshot.colors };
  if (opts) {
    if (opts.font) entry.font = opts.font;
    if (opts.density) entry.density = opts.density;
    if (opts.bgPattern) entry.bgPattern = opts.bgPattern;
    if (opts.bgEffectColor) entry.bgEffectColor = opts.bgEffectColor;
    if (opts.bgEffectIntensity !== undefined) entry.bgEffectIntensity = opts.bgEffectIntensity;
    if (opts.bgEffectSize !== undefined) entry.bgEffectSize = opts.bgEffectSize;
    if (opts.bgEffectControls) entry.bgEffectControls = _copyBackgroundEffectControlValues(opts.bgEffectControls);
    if (opts.frosted !== undefined) entry.frosted = !!opts.frosted;
  }
  ct[name] = entry;
  _saveCustomThemes(ct);
  _syncCustomThemesToServer(ct);
  return 'ok';
}
export function saveCustomTheme(name, colors, opts) {
  const result = _writeCustomThemeEntry(name, colors, opts);
  if (result !== 'ok') return result;
  initThemeUI();
  return 'ok';
}
export function deleteCustomTheme(name) {
  const ct = _loadCustomThemes();
  delete ct[name];
  _saveCustomThemes(ct);
  _syncCustomThemesToServer(ct);
  initThemeUI();
}
function _syncCustomThemesToServer(ct) {
  try {
    fetch('/api/prefs/custom-themes', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ value: ct }),
    }).catch(e => console.warn('Theme sync (custom) failed:', e));
  } catch (e) { console.warn('Theme sync (custom) error:', e); }
}

// --- Syntax color derivation from theme base colors ---
function hexToHSL(hex) {
  const rgb = hexToRgb(hex) || { r: 0, g: 0, b: 0 };
  const r = rgb.r / 255;
  const g = rgb.g / 255;
  const b = rgb.b / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h, s, l = (max + min) / 2;
  if (max === min) { h = s = 0; }
  else {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) h = ((b - r) / d + 2) / 6;
    else h = ((r - g) / d + 4) / 6;
  }
  return [h * 360, s * 100, l * 100];
}

function hslToHex(h, s, l) {
  h = ((h % 360) + 360) % 360;
  s = Math.max(0, Math.min(100, s)) / 100;
  l = Math.max(0, Math.min(100, l)) / 100;
  const a = s * Math.min(l, 1 - l);
  const f = n => { const k = (n + h / 30) % 12; return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)); };
  const toHex = v => Math.round(v * 255).toString(16).padStart(2, '0');
  return '#' + toHex(f(0)) + toHex(f(8)) + toHex(f(4));
}

function deriveSyntaxColors(colors) {
  const [fgH, fgS, fgL] = hexToHSL(colors.fg);
  const [bgH, bgS, bgL] = hexToHSL(colors.bg);
  const [redH, redS, redL] = hexToHSL(colors.red || '#e06c75');
  const isDark = bgL < 50;
  const codeBgL = isDark ? Math.max(bgL - 4, 0) : Math.min(bgL + 4, 100);
  return {
    bg: hslToHex(bgH, bgS, codeBgL),
    fg: colors.fg,
    keyword: hslToHex((redH + 280) % 360, Math.min(redS + 10, 80), isDark ? 70 : 45),
    string: hslToHex(40, Math.min(fgS + 20, 70), isDark ? 72 : 42),
    comment: hslToHex(fgH, Math.max(fgS - 20, 5), isDark ? (fgL * 0.5 + bgL * 0.5) : (fgL * 0.5 + bgL * 0.5)),
    function: hslToHex(210, Math.min(fgS + 20, 75), isDark ? 70 : 45),
    // Extra token colors for richer highlighting
    number: hslToHex(20, Math.min(fgS + 15, 65), isDark ? 68 : 48),
    builtin: hslToHex(180, Math.min(fgS + 15, 60), isDark ? 65 : 40),
    variable: hslToHex((fgH + 30) % 360, Math.min(fgS + 5, 60), isDark ? fgL : fgL),
    params: hslToHex(fgH, Math.max(fgS - 5, 10), isDark ? Math.min(fgL + 8, 85) : Math.max(fgL - 8, 25)),
  };
}

// Advanced picker key → CSS variable mapping
const ADV_KEYS = [
  { key: 'userBubbleBg',       css: '--user-bubble-bg',    label: 'User Chat Bubble', group: 'Chat Bubbles' },
  { key: 'aiBubbleBg',         css: '--ai-bubble-bg',      label: 'AI Chat Bubble',   group: 'Chat Bubbles' },
  { key: 'bubbleBorder',       css: '--bubble-border',     label: 'Border Chat Bubble', group: 'Chat Bubbles' },
  { key: 'sidebarBg',          css: '--sidebar-bg',        label: 'Sidebar Bg',       group: 'Sidebar' },
  { key: 'brandColor',         css: '--brand-color',       label: 'Open Clank Logo',  group: 'Sidebar' },
  { key: 'brandMixTo',         css: '--brand-mix-to',      label: 'Logo Gradient End', group: 'Sidebar' },
  { key: 'hamburgerColor',     css: '--hamburger-color',   label: 'Hamburger Menu',   group: 'Sidebar' },
  { key: 'inputBg',            css: '--input-bg',          label: 'Input Bg',         group: 'Chat Input / Prompt Area' },
  { key: 'inputBorder',        css: '--input-border',      label: 'Input Border',     group: 'Chat Input / Prompt Area' },
  { key: 'sendBtnBg',          css: '--send-btn-bg',       label: 'Send Btn',         group: 'Chat Input / Prompt Area' },
  { key: 'sendBtnHover',       css: '--send-btn-hover',    label: 'Send Hover',       group: 'Chat Input / Prompt Area' },
  { key: 'codeBg',             css: '--code-bg',           label: 'Code Bg',          group: 'Code Blocks' },
  { key: 'codeFg',             css: '--code-fg',           label: 'Code Text',        group: 'Code Blocks' },
  { key: 'toggleActive',       css: '--toggle-active',     label: 'Toggle On',        group: 'Controls' },
];

function computeAdvancedDefaults(colors) {
  const syn = deriveSyntaxColors(colors);
  const red = colors.red || THEME_ACCENT_DEFAULT;
  return {
    userBubbleBg: colors.bg,
    aiBubbleBg: colors.panel,
    bubbleBorder: colors.border,
    sidebarBg: colors.panel,
    brandColor: red,
    brandMixTo: colors.fg,
    hamburgerColor: colors.fg,
    inputBg: colors.panel,
    inputBorder: colors.border,
    sendBtnBg: red,
    sendBtnHover: red,
    codeBg: syn.bg,
    codeFg: syn.fg,
    toggleActive: red,
  };
}

function generateHarmonyColors(accentHex, harmonyType, mode) {
  const [h, s] = hexToHSL(accentHex);
  const isDark = mode === 'dark';

  let bgH, bgS, bgL, fgS, fgL, panelL, borderH, borderS, borderL;

  if (harmonyType === 'complementary') {
    bgH = h; bgS = Math.max(s * 0.15, 3);
    bgL = isDark ? 13 : 95; fgL = isDark ? 85 : 15; fgS = Math.max(s * 0.2, 5);
    panelL = isDark ? 8 : 98;
    borderH = h; borderS = Math.max(s * 0.25, 8); borderL = isDark ? 28 : 75;
  } else if (harmonyType === 'analogous') {
    bgH = (h - 30 + 360) % 360; bgS = Math.max(s * 0.12, 3);
    bgL = isDark ? 14 : 95; fgL = isDark ? 84 : 18; fgS = Math.max(s * 0.15, 5);
    panelL = isDark ? 9 : 97;
    borderH = (h + 30) % 360; borderS = Math.max(s * 0.3, 10); borderL = isDark ? 30 : 72;
  } else if (harmonyType === 'triadic') {
    bgH = (h + 240) % 360; bgS = Math.max(s * 0.1, 2);
    bgL = isDark ? 13 : 96; fgL = isDark ? 86 : 14; fgS = Math.max(s * 0.18, 5);
    panelL = isDark ? 8 : 99;
    borderH = (h + 120) % 360; borderS = Math.max(s * 0.2, 8); borderL = isDark ? 28 : 74;
  } else { // monochromatic
    bgH = h; bgS = Math.max(s * 0.08, 2);
    bgL = isDark ? 12 : 96; fgL = isDark ? 87 : 13; fgS = Math.max(s * 0.15, 5);
    panelL = isDark ? 7 : 99;
    borderH = h; borderS = Math.max(s * 0.2, 6); borderL = isDark ? 26 : 76;
  }

  return {
    bg: hslToHex(bgH, bgS, bgL),
    fg: hslToHex(h, fgS, fgL),
    panel: hslToHex(bgH, bgS * 0.6, panelL),
    border: hslToHex(borderH, borderS, borderL),
    red: accentHex,
  };
}

export function applyColors(colors) {
  // Start from a known palette on every application. In particular, a custom
  // four-color record without `red` must not inherit the previous theme's
  // accent from the document root.
  // Built-in snapshots already contain their complete advanced palette. A
  // custom snapshot must resolve absent advanced colors from its own basic
  // palette, otherwise Clanker-specific values bleed into custom themes.
  const palette = {
    ...(colors && typeof colors === 'object' ? colors : {}),
    advanced: { ...(colors?.advanced || {}) },
  };
  const s = document.documentElement.style;
  s.setProperty('--bg', palette.bg);
  s.setProperty('--fg', palette.fg);
  s.setProperty('--panel', palette.panel);
  s.setProperty('--border', palette.border);
  s.setProperty('--red', palette.red || THEME_ACCENT_DEFAULT);
  const accent = palette.advanced.accentPrimary || palette.red || THEME_ACCENT_DEFAULT;
  const clankerDefault = (document.body.classList.contains('theme-clanker-dark')
    && palette.advanced.brandColor === '#F6BE48')
    || (document.body.classList.contains('theme-clanker-light')
      && palette.advanced.brandColor === '#EC635C');
  const accentHover = palette.advanced.accentHover || palette.advanced.sendBtnHover || (clankerDefault
    ? THEME_ACCENT_HOVER : accent);
  const accentFocus = palette.advanced.accentFocus || (clankerDefault ? THEME_ACCENT_FOCUS : accent);
  const accentDisabled = palette.advanced.accentDisabled || (clankerDefault ? THEME_ACCENT_DISABLED : accent);
  const accentError = palette.advanced.accentError || THEME_ERROR_DEFAULT;
  s.setProperty('--accent', accent);
  s.setProperty('--accent-primary', accent);
  s.setProperty('--accent-hover', accentHover);
  s.setProperty('--accent-focus', accentFocus);
  s.setProperty('--accent-selection', accent);
  s.setProperty('--accent-disabled', accentDisabled);
  s.setProperty('--accent-error', accentError);
  s.setProperty('--color-accent', accent);
  s.setProperty('--color-error', accentError);
  s.setProperty('--color-danger', accentError);
  s.setProperty('--color-success', palette.advanced.successColor || THEME_SUCCESS_DEFAULT);
  // Clanker class rules live on `body` and intentionally provide a complete
  // first-paint fallback. Mirror runtime values there so an explicit custom
  // override still wins over those class defaults.
  const bodyStyle = document.body?.style;
  if (bodyStyle) {
    for (const [key, value] of Object.entries({
      '--red': palette.red || THEME_ACCENT_DEFAULT,
      '--accent': accent, '--accent-primary': accent, '--accent-hover': accentHover,
      '--accent-focus': accentFocus, '--accent-disabled': accentDisabled,
      '--accent-error': accentError, '--color-accent': accent,
      '--color-error': accentError, '--color-danger': accentError,
      '--color-success': palette.advanced.successColor || THEME_SUCCESS_DEFAULT,
    })) bodyStyle.setProperty(key, value);
  }

  // Keep the mobile browser toolbar / status bar matched to the theme bg
  // (same as the early head-script does on first paint).
  const _mtc = document.querySelector('meta[name="theme-color"]');
  if (_mtc && palette.bg) _mtc.setAttribute('content', palette.bg);

  // Derive and apply syntax highlighting colors
  const syn = deriveSyntaxColors(palette);
  s.setProperty('--hl-bg', syn.bg);
  s.setProperty('--hl-fg', syn.fg);
  s.setProperty('--hl-keyword', syn.keyword);
  s.setProperty('--hl-string', syn.string);
  s.setProperty('--hl-comment', syn.comment);
  s.setProperty('--hl-function', syn.function);
  s.setProperty('--hl-number', syn.number);
  s.setProperty('--hl-builtin', syn.builtin);
  s.setProperty('--hl-variable', syn.variable);
  s.setProperty('--hl-params', syn.params);

  // Apply advanced overrides (or defaults)
  const adv = palette.advanced || {};
  const defaults = computeAdvancedDefaults(palette);
  for (const { key, css } of ADV_KEYS) {
    s.setProperty(css, adv[key] || defaults[key]);
  }

  // Update favicon to match theme accent color
  _updateFavicon(palette.red || THEME_ACCENT_DEFAULT);
}

// Per-route SVG shape registry — kept in sync with the inline favicon
// script in index.html so a theme change keeps the route icon, not the
// default project mark. Returns the inner SVG markup colored with `fg`.
const _ROUTE_FAVICON_SHAPES = {
  '/calendar':
    "<rect x='4' y='6' width='24' height='22' rx='2' fill='none' stroke='__C__' stroke-width='2.5'/>" +
    "<line x1='4' y1='12' x2='28' y2='12' stroke='__C__' stroke-width='2.5'/>" +
    "<line x1='10' y1='3' x2='10' y2='9' stroke='__C__' stroke-width='2.5' stroke-linecap='round'/>" +
    "<line x1='22' y1='3' x2='22' y2='9' stroke='__C__' stroke-width='2.5' stroke-linecap='round'/>",
  '/notes':
    "<rect x='6' y='4' width='20' height='24' rx='2' fill='none' stroke='__C__' stroke-width='2.5'/>" +
    "<line x1='10' y1='10' x2='22' y2='10' stroke='__C__' stroke-width='2'/>" +
    "<line x1='10' y1='15' x2='22' y2='15' stroke='__C__' stroke-width='2'/>" +
    "<line x1='10' y1='20' x2='18' y2='20' stroke='__C__' stroke-width='2'/>",
  '/cookbook':
    "<path d='M5 8 L5 26 A2 2 0 0 0 7 28 L25 28 A2 2 0 0 0 27 26 L27 8' fill='none' stroke='__C__' stroke-width='2.5' stroke-linejoin='round'/>" +
    "<path d='M9 4 L23 4 L23 8 L9 8 Z' fill='none' stroke='__C__' stroke-width='2.5' stroke-linejoin='round'/>" +
    "<line x1='11' y1='14' x2='21' y2='14' stroke='__C__' stroke-width='2'/>" +
    "<line x1='11' y1='19' x2='17' y2='19' stroke='__C__' stroke-width='2'/>",
  '/email':
    "<rect x='4' y='7' width='24' height='18' rx='2' fill='none' stroke='__C__' stroke-width='2.5'/>" +
    "<path d='M5 9 L16 17 L27 9' fill='none' stroke='__C__' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>",
  '/memory':
    "<path d='M16 5 C10 5 6 9 6 14 C6 19 10 21 11 22 L11 26 L21 26 L21 22 C22 21 26 19 26 14 C26 9 22 5 16 5 Z' fill='none' stroke='__C__' stroke-width='2.5' stroke-linejoin='round'/>" +
    "<line x1='12' y1='28' x2='20' y2='28' stroke='__C__' stroke-width='2'/>",
  '/gallery':
    "<rect x='4' y='4' width='24' height='24' rx='2' fill='none' stroke='__C__' stroke-width='2.5'/>" +
    "<circle cx='12' cy='12' r='2.5' fill='__C__'/>" +
    "<path d='M4 22 L11 16 L18 21 L23 17 L28 22' fill='none' stroke='__C__' stroke-width='2.5' stroke-linejoin='round'/>",
  '/tasks':
    "<rect x='4' y='4' width='24' height='24' rx='3' fill='none' stroke='__C__' stroke-width='2.5'/>" +
    "<path d='M9 16 L14 21 L23 11' fill='none' stroke='__C__' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>",
  '/library':
    "<rect x='5' y='5' width='5' height='22' rx='1' fill='none' stroke='__C__' stroke-width='2.5'/>" +
    "<rect x='13' y='5' width='5' height='22' rx='1' fill='none' stroke='__C__' stroke-width='2.5'/>" +
    "<rect x='21' y='8' width='6' height='19' rx='1' fill='none' stroke='__C__' stroke-width='2.5' transform='rotate(8 24 17)'/>",
};

function _updateFavicon(fg) {
  const path = (window.location.pathname || '').toLowerCase();
  const routeShape = _ROUTE_FAVICON_SHAPES[path];
  let svg;
  if (routeShape) {
    svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>${routeShape.split('__C__').join(fg)}</svg>`;
  } else {
    svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><path d='M16 3 29 27H3Z' fill='none' stroke='${fg}' stroke-width='2' stroke-linejoin='round'/><path d='M8.5 17Q16 7 23.5 17Q16 27 8.5 17ZM16 13.3a3.7 3.7 0 1 0 0 7.4 3.7 3.7 0 1 0 0-7.4Z' fill='${fg}' fill-rule='evenodd' clip-rule='evenodd'/><circle cx='16' cy='17' r='1.2' fill='${fg}'/></svg>`;
  }
  const href = 'data:image/svg+xml,' + encodeURIComponent(svg);
  let link = document.querySelector("link[rel='icon']");
  if (!link) {
    link = document.createElement('link');
    link.rel = 'icon';
    link.type = 'image/svg+xml';
    document.head.appendChild(link);
  }
  link.href = href;
  let apple = document.querySelector("link[rel='apple-touch-icon']");
  if (!apple) {
    apple = document.createElement('link');
    apple.rel = 'apple-touch-icon';
    document.head.appendChild(apple);
  }
  apple.href = href;
}

// Cache of discovered custom fonts: { "Family Name": [ {file, url, format} ] }
let _customFonts = {};
// Track which custom font families already have @font-face injected
const _injectedFonts = new Set();

function _injectFontFace(familyName, variants) {
  if (_injectedFonts.has(familyName)) return;
  const style = document.createElement('style');
  style.dataset.customFont = familyName;
  const fmtMap = { woff2: 'woff2', woff: 'woff', ttf: 'truetype', otf: 'opentype' };
  for (const v of variants) {
    style.textContent += `@font-face { font-family: '${familyName}'; src: url('${v.url}') format('${fmtMap[v.format] || v.format}'); font-display: swap; }\n`;
  }
  document.head.appendChild(style);
  _injectedFonts.add(familyName);
}

export function applyFontDensity(font, density) {
  const f = font || DEFAULT_FONT;
  const d = density || DEFAULT_DENSITY;
  let family = FONT_MAP[f];
  if (!family && _customFonts[f]) {
    // It's a custom font from the local folder
    _injectFontFace(f, _customFonts[f]);
    family = "'" + f + "', sans-serif";
  }
  if (!family) family = FONT_MAP[DEFAULT_FONT];
  document.documentElement.style.setProperty('--font-family', family);
  document.documentElement.classList.remove('density-compact', 'density-spacious');
  if (d !== 'comfortable') document.documentElement.classList.add('density-' + d);
}

// UI text-size scale (accessibility). Global and independent of the active
// theme, so the chosen size persists across theme switches. Stored as a plain
// percentage string ('100' | '110' | '125' | '150').
const UI_SCALE_KEY = 'odysseus-ui-scale';
const DEFAULT_UI_SCALE = '100';

export function applyUiScale(scale) {
  const s = scale || DEFAULT_UI_SCALE;
  // Only one non-default scale ('125'). Remove any legacy classes too so an
  // older stored value can't leave a stale zoom applied.
  document.documentElement.classList.remove('ui-scale-110', 'ui-scale-125', 'ui-scale-140');
  if (s === '125') document.documentElement.classList.add('ui-scale-125');
}

const _BG_CLASSES = ['bg-pattern-dots', 'bg-pattern-clanker-routefield', 'bg-pattern-clanker-kene-weave',
  'bg-pattern-clanker-lcars', 'bg-pattern-clanker-gem-drift', 'bg-pattern-clanker-emoji-drift',
  'bg-pattern-clanker-matrix-rain', 'bg-pattern-clanker-emoji-rain', 'bg-pattern-clanker-sweep', 'bg-pattern-clanker-blueprint',
  'bg-pattern-synapse', 'bg-pattern-rain', 'bg-pattern-constellations',
  'bg-pattern-perlin-flow',
  'bg-pattern-petals', 'bg-pattern-sparkles', 'bg-pattern-embers'];
const _CANVAS_PATTERNS = { 'clanker-routefield': _initClankerRoutefield,
  'clanker-kene-weave': _initClankerKeneWeave,
  'clanker-lcars': _initClankerLcars,
  'clanker-gem-drift': _initClankerGemDrift,
  'clanker-emoji-drift': _initClankerEmojiDrift,
  'clanker-matrix-rain': _initClankerMatrixRain,
  'clanker-emoji-rain': _initClankerEmojiRain,
  synapse: _initSynapse, rain: _initRain, constellations: _initConstellations,
  'perlin-flow': _initPerlinFlow,
  petals: _initPetals, sparkles: _initSparkles, embers: _initEmbers };
const _BACKGROUND_CANVAS_SELECTOR = '[data-background-effect-canvas], [data-clanker-effect-canvas], #synapse-canvas, #rain-canvas, #constellations-canvas, #perlin-flow-canvas, #petals-canvas, #sparkles-canvas, #embers-canvas';
let _activeBackgroundEffectDispose = null;
// Duplicate module URLs get separate scopes; keep canvas ownership shared.
const _BACKGROUND_OWNER_KEY = '__openClankBackgroundOwner';
let _clankerPaletteEnabled = false;
const _BACKGROUND_EFFECT_CONTROLS = new Map();
let _backgroundEffectControlValues = {};

function _copyBackgroundEffectControlValues(values = _backgroundEffectControlValues) {
  if (!values || typeof values !== 'object') return {};
  return Object.fromEntries(Object.entries(values)
    .filter(([, controls]) => controls && typeof controls === 'object')
    .map(([pattern, controls]) => [pattern, Object.fromEntries(
      Object.entries(controls).filter(([key]) => !!_backgroundEffectControl(pattern, key)),
    )]));
}

function _backgroundEffectControlsFor(pattern) {
  return _BACKGROUND_EFFECT_CONTROLS.get(pattern) || [];
}

function _backgroundEffectControl(pattern, key) {
  return _backgroundEffectControlsFor(pattern).find(control => control.key === key);
}

function _normalizeBackgroundEffectControlValue(control, value) {
  if (control?.type === 'toggle') {
    if (value === undefined || value === null) return !!control.default;
    return value === true || value === 'true' || value === 1;
  }
  const min = Number.isFinite(control?.min) ? control.min : 0;
  const max = Number.isFinite(control?.max) ? control.max : 100;
  const fallback = Number.isFinite(control?.default) ? control.default : min;
  const candidate = Number(value);
  return Math.min(max, Math.max(min, Number.isFinite(candidate) ? candidate : fallback));
}

export function getBackgroundEffectControlValue(pattern, key, fallback) {
  const control = _backgroundEffectControl(pattern, key);
  if (!control) return fallback;
  return _normalizeBackgroundEffectControlValue(control, _backgroundEffectControlValues[pattern]?.[key]);
}

function _setBackgroundEffectControlValue(pattern, key, value) {
  const control = _backgroundEffectControl(pattern, key);
  if (!control) return;
  _backgroundEffectControlValues = _copyBackgroundEffectControlValues();
  _backgroundEffectControlValues[pattern] = { ..._backgroundEffectControlValues[pattern] };
  _backgroundEffectControlValues[pattern][key] = _normalizeBackgroundEffectControlValue(control, value);
}

function _persistBackgroundEffectControls() {
  const saved = getSaved();
  const name = saved?.name || DEFAULT_THEME;
  const colors = saved?.colors || THEMES[name] || THEMES[DEFAULT_THEME];
  const opts = _getThemeOptions(name, saved || {});
  const pattern = document.getElementById('theme-bg-pattern-select');
  const effectColor = document.getElementById('theme-bg-effect-color');
  const intensity = document.getElementById('theme-bg-intensity');
  const size = document.getElementById('theme-bg-size');
  if (pattern) opts.bgPattern = pattern.value;
  if (effectColor) opts.bgEffectColor = effectColor.value;
  if (intensity) opts.bgEffectIntensity = Number(intensity.value) / 100;
  if (size) opts.bgEffectSize = Number(size.value) / 100;
  opts.bgEffectControls = _copyBackgroundEffectControlValues();
  save(name, colors, opts);
}

function _backgroundEffectControlHost() {
  if (typeof document === 'undefined') return null;
  let host = document.getElementById('theme-bg-effect-controls');
  if (host) return host;
  const anchor = document.getElementById('theme-bg-size-group')?.closest('.theme-fd-row');
  if (!anchor) return null;
  host = document.createElement('div');
  host.id = 'theme-bg-effect-controls';
  host.className = 'theme-effect-controls';
  anchor.insertAdjacentElement('afterend', host);
  return host;
}

function _formatBackgroundEffectControlValue(control, value) {
  return `${value}${control.unit || ''}`;
}

function _renderBackgroundEffectControls(pattern = _activeBgPattern()) {
  const host = _backgroundEffectControlHost();
  if (!host) return;
  const controls = _backgroundEffectControlsFor(pattern);
  const advancedSettings = getBackgroundEffectControlValue(pattern, 'advancedSettings', false);
  host.replaceChildren();
  host.hidden = controls.length === 0;
  for (const control of controls) {
    if (control.key !== 'advancedSettings' && control.advanced && !advancedSettings) continue;
    const when = control.showWhen;
    if (when && getBackgroundEffectControlValue(pattern, when.key, false) !== when.value) continue;
    const id = `theme-bg-effect-${pattern}-${control.key}`;
    const value = getBackgroundEffectControlValue(pattern, control.key, control.default);
    const group = document.createElement('div');
    group.className = 'theme-effect-control';
    if (control.type === 'toggle') {
      group.classList.add('theme-effect-toggle');
      const label = document.createElement('label');
      label.className = 'theme-fd-label';
      label.htmlFor = id;
      label.textContent = control.label;
      const toggle = document.createElement('label');
      toggle.className = 'admin-switch';
      const input = document.createElement('input');
      input.id = id;
      input.type = 'checkbox';
      input.checked = value;
      input.setAttribute('aria-label', control.label);
      const slider = document.createElement('span');
      slider.className = 'admin-slider';
      toggle.append(input, slider);
      input.addEventListener('change', () => {
        _setBackgroundEffectControlValue(pattern, control.key, input.checked);
        _persistBackgroundEffectControls();
        _renderBackgroundEffectControls(pattern);
        _invalidateBackgroundPaint();
      });
      group.append(label, toggle);
    } else {
      const label = document.createElement('label');
      label.className = 'theme-fd-label theme-effect-control-label';
      label.htmlFor = id;
      const name = document.createElement('span');
      name.textContent = control.label;
      const output = document.createElement('output');
      output.textContent = _formatBackgroundEffectControlValue(control, value);
      label.append(name, output);
      const input = document.createElement('input');
      input.id = id;
      input.className = 'theme-fd-range';
      input.type = 'range';
      input.min = String(control.min);
      input.max = String(control.max);
      input.step = String(control.step);
      input.value = String(value);
      input.setAttribute('aria-label', control.label);
      input.addEventListener('input', () => {
        _setBackgroundEffectControlValue(pattern, control.key, input.value);
        output.textContent = _formatBackgroundEffectControlValue(control, input.value);
        _invalidateBackgroundPaint();
      });
      input.addEventListener('change', _persistBackgroundEffectControls);
      group.append(label, input);
    }
    host.append(group);
  }
}

export function registerBackgroundEffectControls(pattern, controls) {
  if (!pattern || !Array.isArray(controls)) return;
  const keys = new Set();
  const normalized = controls.filter(control => {
    if (!control || typeof control.key !== 'string' || keys.has(control.key)) return false;
    keys.add(control.key);
    return true;
  })
    .map(control => {
      const min = Number.isFinite(Number(control.min)) ? Number(control.min) : 0;
      const max = Math.max(min, Number.isFinite(Number(control.max)) ? Number(control.max) : 100);
      return {
        ...control,
        type: control.type === 'toggle' ? 'toggle' : 'range',
        min,
        max,
        step: Number.isFinite(Number(control.step)) ? Number(control.step) : 1,
        default: control.type === 'toggle' ? !!control.default : Math.min(max, Math.max(min,
          Number.isFinite(Number(control.default)) ? Number(control.default) : min)),
      };
    });
  _BACKGROUND_EFFECT_CONTROLS.set(pattern, normalized);
  if (typeof document !== 'undefined') _renderBackgroundEffectControls();
}

export function applyBackgroundEffectControls(values) {
  _backgroundEffectControlValues = _copyBackgroundEffectControlValues(values);
  if (typeof document !== 'undefined') _renderBackgroundEffectControls();
}

const _CLANKER_ADVANCED_SETTINGS = {
  key: 'advancedSettings', label: 'Advanced settings', type: 'toggle', default: false,
};

function _withClankerAdvancedSettings(controls) {
  return [_CLANKER_ADVANCED_SETTINGS, ...controls.map(control => ({ ...control, advanced: true }))];
}

if (typeof document !== 'undefined') registerBackgroundEffectControls('clanker-kene-weave', [
  _CLANKER_ADVANCED_SETTINGS,
  { key: 'snakeCount', label: 'Number of snakes', min: 1, max: 64, step: 1, default: 7, advanced: true },
  { key: 'snakeSpeed', label: 'Snake speed', min: 25, max: 500, step: 5, default: 100, unit: '%', advanced: true },
  { key: 'snakeSpeedVariationToggle', label: 'Vary speed per snake', type: 'toggle', default: false, advanced: true },
  { key: 'snakeSpeedVariation', label: 'Speed variation amount', min: 0, max: 100, step: 5, default: 30, unit: '%', advanced: true, showWhen: { key: 'snakeSpeedVariationToggle', value: true } },
  { key: 'snakePaintTrail', label: 'Paint trail on grid', type: 'toggle', default: false, advanced: true },
  { key: 'snakeLengthVariation', label: 'Snake length variation', min: 0, max: 100, step: 5, default: 0, unit: '%', advanced: true },
  { key: 'snakeLifetimeVariation', label: 'Snake lifetime variation', min: 0, max: 100, step: 5, default: 0, unit: '%', advanced: true },
  { key: 'shorterLastLonger', label: 'Shorter snakes last longer', type: 'toggle', default: false, advanced: true },
  { key: 'shorterLifetimeScale', label: 'Short snake lifetime scale', min: 0, max: 200, step: 5, default: 100, unit: '%', advanced: true, showWhen: { key: 'shorterLastLonger', value: true } },
  { key: 'longerDisappearSooner', label: 'Longer snakes disappear sooner', type: 'toggle', default: false, advanced: true },
  { key: 'longerLifetimeScale', label: 'Long snake lifetime scale', min: 0, max: 200, step: 5, default: 100, unit: '%', advanced: true, showWhen: { key: 'longerDisappearSooner', value: true } },
]);

const _CLANKER_DRIFT_CONTROLS = [
  { key: 'driftSpeed', label: 'Drift speed', min: 25, max: 500, step: 5, default: 100, unit: '%' },
  { key: 'driftSpeedVariation', label: 'Drift speed variation', min: 0, max: 100, step: 1, default: 20, unit: '%' },
  { key: 'gemSizeVariation', label: 'Size variation', min: 0, max: 999, step: 1, default: 100, unit: '%' },
  { key: 'intensityVariation', label: 'Intensity variation', min: 0, max: 999, step: 1, default: 100, unit: '%' },
  { key: 'middleIntensity', label: 'Middle intensity', min: 0, max: 200, step: 1, default: 100, unit: '%' },
  { key: 'totalQuantity', label: 'Total quantity', min: 0, max: 250, step: 5, default: 100, unit: '%' },
  { key: 'glowLikelihood', label: 'Glow likelihood', min: 0, max: 100, step: 1, default: 11, unit: '%' },
  { key: 'rotationLikelihood', label: 'Rotation likelihood', min: 0, max: 100, step: 1, default: 35, unit: '%' },
  { key: 'rotationSpeed', label: 'Rotation speed', min: 0, max: 500, step: 5, default: 100, unit: '%' },
  { key: 'rotationSpeedVariation', label: 'Rotation speed variation', min: 0, max: 100, step: 1, default: 30, unit: '%' },
];

registerBackgroundEffectControls('clanker-gem-drift', _withClankerAdvancedSettings(_CLANKER_DRIFT_CONTROLS));
registerBackgroundEffectControls('clanker-emoji-drift', _withClankerAdvancedSettings(_CLANKER_DRIFT_CONTROLS));

const _CLANKER_CODE_RAIN_CONTROLS = [
  { key: 'splashQuantity', label: 'Quantity', min: .4, max: 8, step: .1, default: 1.4, unit: 'x' },
  { key: 'splashRainSpeed', label: 'Rain speed', min: .1, max: 4, step: .1, default: 1, unit: 'x' },
  { key: 'splashRainFlicker', label: 'Flicker', min: 0, max: 1, step: .01, default: .16 },
  { key: 'splashRainSpread', label: 'Spread', min: 0, max: 4, step: .05, default: 1, unit: 'x' },
  { key: 'splashRainDown', label: 'Rain downward', type: 'toggle', default: true },
  { key: 'splashRainReverseChance', label: 'Reverse chance', min: 0, max: 100, step: .1, default: 0, unit: '%' },
  { key: 'splashRainWaves', label: 'Spawn in waves', type: 'toggle', default: false },
  { key: 'splashCharVariety', label: 'Character variety', min: .05, max: 1, step: .05, default: 1 },
  { key: 'splashMinOpacity', label: 'Minimum opacity', min: .02, max: 1, step: .02, default: .2 },
  { key: 'splashMaxOpacity', label: 'Maximum opacity', min: .02, max: 1, step: .02, default: 1 },
  { key: 'splashSizeVariance', label: 'Size variance', min: 0, max: 1.4, step: .05, default: .45 },
  // S24: full-palette rain stays on; this control only adds extra variation
  // on top of the palette (T14: label promises "extra palette variation", not
  // that disabling it removes color from the rain).
  { key: 'splashColorVarianceEnabled', label: 'Extra palette variation', type: 'toggle', default: false },
  { key: 'splashColorVariance', label: 'Extra palette variation amount', min: 0, max: 1, step: .05, default: .32 },
  // S24: separate rare-effect toggle. Default ON. Sampled once per spawn at
  // 1/10,000 — never the coarse reverse-chance slider.
  { key: 'splashRareUpward', label: 'Rare upward drop (1 in 10,000)', type: 'toggle', default: true },
  { key: 'splashBounce', label: 'Bounce force', min: 0, max: 3, step: .1, default: 1.3 },
  { key: 'splashGravity', label: 'Gravity', min: .1, max: 4, step: .1, default: .2 },
  { key: 'splashCollisionForce', label: 'Collision force', min: 0, max: 4, step: .1, default: 2.2 },
];

registerBackgroundEffectControls('clanker-matrix-rain', [
  _CLANKER_ADVANCED_SETTINGS,
  ...[
    ..._CLANKER_CODE_RAIN_CONTROLS,
    { key: 'splashEmojiMix', label: 'Rare emoji mix', type: 'toggle', default: false },
    { key: 'splashEmojiRarity', label: 'Emoji rarity (1 in)', min: 1, max: 10000000, step: 1, default: 10000000, showWhen: { key: 'splashEmojiMix', value: true } },
  ].map(control => ({ ...control, advanced: true })),
]);
registerBackgroundEffectControls('clanker-emoji-rain', _withClankerAdvancedSettings(_CLANKER_CODE_RAIN_CONTROLS));

function _disposeBackgroundEffect() {
  const dispose = window[_BACKGROUND_OWNER_KEY] || _activeBackgroundEffectDispose;
  _activeBackgroundEffectDispose = null;
  if (window[_BACKGROUND_OWNER_KEY] === dispose) window[_BACKGROUND_OWNER_KEY] = null;
  if (dispose) dispose();
  document.querySelectorAll(_BACKGROUND_CANVAS_SELECTOR).forEach(canvas => {
    if (typeof canvas.__disposeEffect === 'function') canvas.__disposeEffect();
    else canvas.remove();
  });
}

// T10: under reduced motion the RAF loop is intentionally stopped, so a style
// change would otherwise leave a stale painted frame. Ask the active background
// canvases to paint exactly one frame.
export function _invalidateBackgroundPaint() {
  if (typeof document === 'undefined') return;
  document.querySelectorAll(_BACKGROUND_CANVAS_SELECTOR).forEach(canvas => {
    if (typeof canvas.__requestBackgroundRepaint === 'function') canvas.__requestBackgroundRepaint();
    else if (typeof canvas.__backgroundPaint === 'function') canvas.__backgroundPaint();
  });
}

export function applyBgEffectColor(color) {
  document.documentElement.style.setProperty('--bg-effect-color', color || '');
  _invalidateBackgroundPaint();
}

export function applyBgEffectIntensity(v) {
  // v is 0..1. Default 1 (full intensity) when missing.
  const n = (v === undefined || v === null || isNaN(v)) ? 1 : Math.max(0, Math.min(1, Number(v)));
  document.documentElement.style.setProperty('--bg-effect-intensity', String(n));
  _invalidateBackgroundPaint();
}

export function applyBgEffectSize(v) {
  // v is a multiplier 0.3..2.5. Default 1 when missing.
  const n = (v === undefined || v === null || isNaN(v)) ? 1 : Math.max(0.2, Math.min(3, Number(v)));
  document.documentElement.style.setProperty('--bg-effect-size', String(n));
  document.documentElement.style.setProperty('--clanker-grid-size', `${Math.round(32 * n)}px`);
  document.documentElement.style.setProperty('--clanker-route-size', `${Math.round(160 * n)}px`);
  _invalidateBackgroundPaint();
}

/** Toggle the global "frosted glass" look — applies a translucent + blurred
 *  treatment to every panel, sidebar, modal, dropdown, and popover via CSS
 *  rules scoped to `body.theme-frosted`. */
export function applyFrostedGlass(on) {
  document.body.classList.toggle('theme-frosted', !!on);
}

// Read current size multiplier for JS effects (canvas-based).
function _getEffectSize() {
  const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--bg-effect-size'));
  return isNaN(v) ? 1 : v;
}

// T17: dots paints with `--bg-effect-intensity`, so its intensity control must
// stay discoverable. Size has no visible effect on the fixed dots tile, and
// "none" has neither — hide only the controls that cannot do anything.
const _NO_INTENSITY_PATTERNS = new Set(['none']);
const _NO_SIZE_PATTERNS = new Set(['none', 'dots']);

function _activeBgPattern() {
  const activeClass = _BG_CLASSES.find(className => document.body.classList.contains(className));
  return activeClass ? activeClass.slice('bg-pattern-'.length) : 'none';
}

function _syncBgPatternControlVisibility(pattern) {
  const ig = document.getElementById('theme-bg-intensity-group');
  const sg = document.getElementById('theme-bg-size-group');
  if (ig) ig.style.display = _NO_INTENSITY_PATTERNS.has(pattern) ? 'none' : '';
  if (sg) sg.style.display = _NO_SIZE_PATTERNS.has(pattern) ? 'none' : '';
}

export function applyBgPattern(pattern) {
  const p = pattern || 'none';
  const samePattern = _activeBgPattern() === p;
  const hasActiveOwner = window[_BACKGROUND_OWNER_KEY] || _activeBackgroundEffectDispose;
  if (samePattern && (!_CANVAS_PATTERNS[p] || hasActiveOwner)) {
    _syncBgPatternControlVisibility(p);
    _renderBackgroundEffectControls(p);
    return;
  }
  document.body.classList.remove(..._BG_CLASSES);
  _disposeBackgroundEffect();
  if (p !== 'none') document.body.classList.add('bg-pattern-' + p);
  if (_CANVAS_PATTERNS[p]) _CANVAS_PATTERNS[p]();
  _syncBgPatternControlVisibility(p);
  _renderBackgroundEffectControls(p);
}

export function getSaved() {
  const raw = Storage.getJSON(_themeOwnerKey(), null);
  const snapshot = normalizeThemeSnapshot(raw, { accountId: _themeOwner });
  if (!snapshot) return null;
  // Idempotent migration: once an old record is read, persist the canonical
  // shape under its current owner. The legacy key is only used before auth.
  try {
    const serialized = JSON.stringify(snapshot);
    if (JSON.stringify(raw) !== serialized) Storage.setJSON(_themeOwnerKey(), snapshot);
  } catch (_) { /* storage quota/private mode is non-fatal */ }
  return snapshot;
}

export function save(name, colors, opts) {
  const source = { name, colors, ...(opts || {}), accountId: _themeOwner };
  const snapshot = normalizeThemeSnapshot(source, { name, accountId: _themeOwner, preserveExplicit: true });
  if (!snapshot) return null;
  Storage.setJSON(_themeOwnerKey(), snapshot);
  // Anonymous/legacy callers still use the original key. Once auth has
  // established an owner, only the namespaced key is durable.
  if (!_themeOwner) Storage.setJSON(LS_KEY, snapshot);
  _syncToServer(snapshot);
  return snapshot;
}

function _syncToServer(obj) {
  try {
    fetch('/api/prefs/theme', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ value: obj }),
    }).catch(e => console.warn('Theme sync failed:', e));
  } catch (e) { console.warn('Theme sync error:', e); }
}

export function applyThemeIdentity(name) {
  _clankerPaletteEnabled = name === 'clanker-dark' || name === 'clanker-light';
  document.body.classList.remove('theme-clanker-dark', 'theme-clanker-light');
  if (_clankerPaletteEnabled) {
    document.body.classList.add('theme-' + name);
    document.documentElement.style.setProperty('color-scheme', name === 'clanker-light' ? 'light' : 'dark');
  } else {
    document.documentElement.style.removeProperty('color-scheme');
  }
}

function _getThemeOptions(name, source = {}) {
  const snapshot = normalizeThemeSnapshot({ name, colors: source.colors || THEMES[name], ...source }, { name });
  if (!snapshot) return {
    font: DEFAULT_FONT, density: DEFAULT_DENSITY, bgPattern: 'none',
    bgEffectColor: '', bgEffectIntensity: 1, bgEffectSize: 1,
    bgEffectControls: {}, frosted: false,
  };
  return {
    font: snapshot.typography.font,
    density: snapshot.typography.density,
    bgPattern: snapshot.background.pattern,
    bgEffectColor: snapshot.background.effectColor,
    bgEffectIntensity: snapshot.background.effectIntensity,
    bgEffectSize: snapshot.background.effectSize,
    bgEffectControls: _copyBackgroundEffectControlValues(snapshot.background.controls),
    frosted: snapshot.background.frosted,
  };
}

function _syncThemeControls(name, colors, opts) {
  const values = {
    'theme-font-select': opts.font,
    'theme-density-select': opts.density,
    'theme-bg-pattern-select': opts.bgPattern,
    'theme-bg-effect-color': opts.bgEffectColor || colors.fg || '#9cdef2',
    'theme-bg-intensity': String(Math.round(opts.bgEffectIntensity * 100)),
    'theme-bg-size': String(Math.round(opts.bgEffectSize * 100)),
  };
  for (const [id, value] of Object.entries(values)) {
    const el = document.getElementById(id);
    if (el) el.value = value;
  }
  const font = document.getElementById('theme-font-select');
  if (font) {
    const locked = !!THEME_DEFAULT_FONT[name];
    font.disabled = locked;
    font.title = locked ? 'Clanker themes bundle and lock Liga Comic Mono' : '';
  }
  const frosted = document.getElementById('theme-frosted-toggle');
  if (frosted) frosted.checked = opts.frosted;
  if (document.getElementById('clr-bg')) syncPickers(colors);
  document.querySelectorAll('.theme-swatch').forEach(sw => {
    sw.classList.toggle('active', sw.dataset.theme === name);
  });
}

export function applyTheme(name, providedColors = null, config = {}) {
  const custom = _loadCustomThemes();
  const builtIn = Object.prototype.hasOwnProperty.call(THEMES, name);
  const colors = builtIn ? (providedColors || THEMES[name]) : (providedColors || custom[name]);
  if (!colors) return null;
  const optionSource = config.storedOptions || (builtIn ? {} : (custom[name] || colors));
  const snapshot = normalizeThemeSnapshot({ name, colors, ...optionSource }, { name, accountId: _themeOwner });
  if (!snapshot) return null;
  const normalizedColors = snapshot.colors;
  const opts = {
    ...snapshot.background,
    bgPattern: snapshot.background.pattern,
    bgEffectColor: snapshot.background.effectColor,
    bgEffectIntensity: snapshot.background.effectIntensity,
    bgEffectSize: snapshot.background.effectSize,
    bgEffectControls: snapshot.background.controls,
    font: snapshot.typography.font,
    density: snapshot.typography.density,
  };
  applyColors(normalizedColors);
  applyThemeIdentity(name);
  applyFontDensity(opts.font, opts.density);
  applyBgEffectColor(opts.bgEffectColor);
  applyBgEffectIntensity(opts.bgEffectIntensity);
  applyBgEffectSize(opts.bgEffectSize);
  applyBackgroundEffectControls(opts.bgEffectControls);
  applyFrostedGlass(opts.frosted);
  applyBgPattern(opts.bgPattern);
  _syncThemeControls(name, normalizedColors, opts);
  if (config.persist !== false) save(name, normalizedColors, opts);
  return { colors: normalizedColors, opts, snapshot };
}

async function _loadFromServer() {
  try {
    const res = await fetch('/api/prefs/theme', { credentials: 'same-origin' });
    const data = await res.json();
    return data.value || null;
  } catch { return null; }
}

function _applySnapshot(snapshot, persist = false) {
  const normalized = normalizeThemeSnapshot(snapshot, { accountId: _themeOwner });
  if (!normalized) return null;
  return applyTheme(normalized.name, normalized.colors, {
    persist,
    storedOptions: normalized,
  });
}

function _migrateLegacyOwnerState(username, owner) {
  if (!owner || !username || String(Storage.get(AUTH_USER_KEY, '')).trim() !== String(username).trim()) return;
  const ownerKey = _themeOwnerKey(owner);
  if (!Storage.getJSON(ownerKey, null)) {
    const legacy = Storage.getJSON(LS_KEY, null);
    const migrated = normalizeThemeSnapshot(legacy, { accountId: owner });
    if (migrated) Storage.setJSON(ownerKey, migrated);
  }
  const customKey = _customThemeStorageKey(owner);
  if (!Storage.getJSON(customKey, null)) {
    const legacyCustom = Storage.getJSON(CUSTOM_THEMES_KEY, null);
    if (_isPlainObject(legacyCustom)) Storage.setJSON(customKey, legacyCustom);
  }
}

async function _hydrateForAccount(detail = {}) {
  const username = String(detail.username || '').trim();
  const accountId = String(detail.accountId || detail.account_id || '').trim();
  const owner = accountId || username;
  const generation = ++_themeHydrationGeneration;
  _themeOwner = owner || null;
  _migrateLegacyOwnerState(username, owner);

  // Clear the previous account's visual state before waiting on the network.
  const local = getSaved();
  if (local) _applySnapshot(local, false);
  else _applySnapshot(normalizeThemeSnapshot({ name: DEFAULT_THEME }, { accountId: owner }), false);

  if (!owner) return;
  const hydration = (async () => {
    const [themeResult, customResult] = await Promise.allSettled([
      _loadFromServer(),
      fetch('/api/prefs/custom-themes', { credentials: 'same-origin' }).then(response => response.json()),
    ]);
    if (generation !== _themeHydrationGeneration || _themeOwner !== owner) return;
    const serverTheme = themeResult.status === 'fulfilled' ? themeResult.value : null;
    if (serverTheme) {
      const normalized = normalizeThemeSnapshot(serverTheme, { accountId: owner });
      if (normalized) {
        Storage.setJSON(_themeOwnerKey(owner), normalized);
        _applySnapshot(normalized, false);
      }
    }
    if (customResult.status === 'fulfilled' && _isPlainObject(customResult.value?.value)) {
      // A successful owner response is authoritative. Do not merge names from
      // another account into this account's local custom-theme namespace.
      Storage.setJSON(_customThemeStorageKey(owner), customResult.value.value);
      initThemeUI();
    }
    document.dispatchEvent(new CustomEvent('openclank:theme-account-hydrated', {
      detail: { username, accountId: owner, generation },
    }));
  })();
  _themeHydrationPromise = hydration;
  await hydration;
  if (_themeHydrationPromise === hydration) _themeHydrationPromise = null;
}

// init.js emits both legacy and context events during boot. They describe one
// owner transition, so share the in-flight request and avoid duplicate local
// resets/network reads. A changed owner still starts a fresh generation.
function _requestAccountHydration(detail = {}) {
  const username = String(detail.username || '').trim();
  const owner = String(detail.accountId || detail.account_id || username).trim();
  const key = `${owner}\u0000${username}`;
  if (_themeHydrationRequest?.key === key) return _themeHydrationRequest.promise;
  const promise = _hydrateForAccount(detail);
  _themeHydrationRequest = { key, promise };
  promise.then(() => {
    if (_themeHydrationRequest?.promise === promise) _themeHydrationRequest = null;
  }, () => {
    if (_themeHydrationRequest?.promise === promise) _themeHydrationRequest = null;
  });
  return promise;
}

// The theme controls have one DOM owner.  During the Settings rollout the
// existing popup shell remains in the document for legacy callers, but its
// live control tree is reparented into Settings → Appearance.  This keeps
// every color input/listener/ID unique while old launchers still work.
function _mountThemeSettingsSurface() {
  const popup = document.getElementById('theme-popup');
  const host = document.getElementById('settings-theme-controls');
  const modal = document.getElementById('theme-modal');
  if (!popup || !host || !modal) return false;
  if (popup.parentElement !== host) host.appendChild(popup);
  popup.classList.add('settings-theme-surface-content');
  modal.classList.add('settings-theme-bridge');
  modal.classList.add('hidden');
  if (modal.dataset.settingsThemeObserver !== '1') {
    modal.dataset.settingsThemeObserver = '1';
    new MutationObserver(() => {
      if (modal.classList.contains('hidden')) return;
      modal.classList.add('hidden');
      if (window.settingsModule?.open) window.settingsModule.open('appearance');
    }).observe(modal, { attributes: true, attributeFilter: ['class'] });
  }
  return true;
}


function syncPickers(colors) {
  document.getElementById('clr-bg').value = colors.bg;
  document.getElementById('clr-fg').value = colors.fg;
  document.getElementById('clr-panel').value = colors.panel;
  document.getElementById('clr-border').value = colors.border;
  document.getElementById('clr-red').value = colors.red;
  syncAdvancedPickers(colors);
}


function syncAdvancedPickers(colors) {
  const adv = colors.advanced || {};
  const defaults = computeAdvancedDefaults(colors);
  for (const { key } of ADV_KEYS) {
    const el = document.getElementById('adv-' + key);
    if (el) el.value = adv[key] || defaults[key];
  }
}

export function initThemeUI() {
  _mountThemeSettingsSurface();
  const themePopup = document.getElementById('theme-popup');
  const themeHeader = document.getElementById('theme-popup-header');
  if (themePopup && themeHeader && !themePopup.dataset.dragWired) {
    themePopup.dataset.dragWired = '1';
    makeDraggable(themePopup, themeHeader);
  }

  // Attach the in-house color picker to every color input in the theme panel.
  // Safe to call repeatedly — the picker marks inputs it's already wrapped.
  try { initColorPickers(document); } catch (e) { console.warn('Color picker init failed', e); }

  // Populate the advanced color inputs with their computed defaults right now.
  // BUG FIX: without this, untouched inputs sat at the browser-default `#000000`
  // until the user clicked a swatch; the first edit of ANY advanced input then
  // tripped readAdvanced() into storing every other `#000000` as an override —
  // e.g. editing Chat Bubble Border turned Sidebar Bg pure black.
  try {
    const saved = getSaved();
    if (saved && saved.colors) {
      syncAdvancedPickers(saved.colors);
    }
  } catch (e) { console.warn('syncAdvancedPickers on init failed', e); }
  // Wire up theme tabs (Themes / Customize)
  const themeTabs = document.getElementById('theme-tabs');
  if (themeTabs && themeTabs.dataset.themeTabsBound !== '1') {
    themeTabs.dataset.themeTabsBound = '1';
    themeTabs.addEventListener('click', (e) => {
      const tab = e.target.closest('.admin-tab');
      if (!tab) return;
      const targetId = tab.dataset.tab;
      themeTabs.querySelectorAll('.admin-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      document.querySelectorAll('.theme-tab-panel').forEach(p => p.style.display = 'none');
      const panel = document.getElementById(targetId);
      if (panel) panel.style.display = '';
      // Show the opacity slider only on the Customize tab.
      const opWrap = document.getElementById('theme-opacity-wrap');
      if (opWrap) opWrap.classList.toggle('hidden', targetId !== 'theme-tab-customize');
      // Restore full opacity / blur on every other tab. The slider's effect
      // is meant to be Customize-only — peeking at the page while tweaking
      // colors — so swapping back to Themes (or Schedule) should look
      // exactly like the rest of the app's modals again.
      const popup = document.getElementById('theme-popup');
      if (popup) {
        if (targetId === 'theme-tab-customize') {
          // Reapply the Peek toggle's current state.
          if (opWrap && opWrap._apply) opWrap._apply();
        } else {
          popup.style.removeProperty('opacity');
          popup.style.removeProperty('background');
          popup.style.removeProperty('backdrop-filter');
          popup.style.removeProperty('-webkit-backdrop-filter');
          popup.querySelectorAll('.admin-card').forEach(c => {
            c.style.removeProperty('background');
            c.style.removeProperty('backdrop-filter');
            c.style.removeProperty('-webkit-backdrop-filter');
          });
        }
      }
    });
  }


  // Wire the "Peek" opacity toggle — fades the theme modal so the user can
  // see the page behind it while tweaking colors on the Customize tab.
  // On/off only (no slider); starts off, lives in the title bar, and is
  // cleared when the user swaps to Themes / Schedule.
  (function _wireOpacityToggle() {
    const toggle = document.getElementById('theme-opacity-wrap');
    const popup = document.getElementById('theme-popup');
    if (!toggle || !popup || toggle.dataset.bound === '1') return;
    toggle.dataset.bound = '1';
    const PEEK = 55; // % opacity when peeking
    const apply = (on) => {
      const cards = popup.querySelectorAll('.admin-card');
      if (on) {
        // Fade the modal + each inner card via color-mix — never element
        // opacity, so text, controls and swatches stay sharp.
        const bgMix    = `color-mix(in srgb, var(--bg)    ${PEEK}%, transparent)`;
        const panelMix = `color-mix(in srgb, var(--panel) ${PEEK}%, transparent)`;
        popup.style.setProperty('background', bgMix, 'important');
        popup.style.setProperty('backdrop-filter', 'none', 'important');
        popup.style.setProperty('-webkit-backdrop-filter', 'none', 'important');
        popup.style.removeProperty('opacity');
        cards.forEach(c => {
          c.style.setProperty('background', panelMix, 'important');
          c.style.setProperty('backdrop-filter', 'none', 'important');
          c.style.setProperty('-webkit-backdrop-filter', 'none', 'important');
        });
      } else {
        popup.style.removeProperty('opacity');
        popup.style.removeProperty('background');
        popup.style.removeProperty('backdrop-filter');
        popup.style.removeProperty('-webkit-backdrop-filter');
        cards.forEach(c => {
          c.style.removeProperty('background');
          c.style.removeProperty('backdrop-filter');
          c.style.removeProperty('-webkit-backdrop-filter');
        });
      }
    };
    // Expose so the tab-switch handler can reapply when returning to Customize.
    toggle._apply = () => apply(toggle.classList.contains('active'));
    toggle.addEventListener('click', () => {
      const on = !toggle.classList.contains('active');
      toggle.classList.toggle('active', on);
      toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
      apply(on);
    });
  })();

  const grid = document.getElementById('themeGrid');
  if (!grid) return;

  const saved = getSaved();
  const activeName = saved ? saved.name : DEFAULT_THEME;
  const customThemes = _loadCustomThemes();

  // Render preset swatches
  grid.innerHTML = Object.entries(THEMES).map(([name, c]) => `
    <div class="theme-swatch${name === activeName ? ' active' : ''}" data-theme="${name}">
      <div class="theme-swatch-colors">
        <span style="background:${c.bg}"></span>
        <span style="background:${c.panel}"></span>
        <span style="background:${c.fg}"></span>
        <span style="background:${c.red}"></span>
      </div>
      ${THEME_LABELS[name] || name}
    </div>
  `).join('');

  // Render custom theme swatches into separate card
  const userGrid = document.getElementById('themeUserGrid');
  const userCard = document.getElementById('themeUserCard');
  const customEntries = Object.entries(customThemes);
  if (customEntries.length > 0 && userGrid && userCard) {
    userCard.style.display = '';
    userGrid.innerHTML = customEntries.map(([name, c]) => `
      <div class="theme-swatch${name === activeName ? ' active' : ''}" data-theme="${name}" data-custom="1">
        <div class="theme-swatch-colors">
          <span style="background:${c.bg}"></span>
          <span style="background:${c.panel}"></span>
          <span style="background:${c.fg}"></span>
          <span style="background:${c.red}"></span>
        </div>
        <span class="theme-swatch-name">${name}</span>
        <button type="button" class="theme-delete-btn" data-delete="${name}" title="Delete theme"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>
      </div>
    `).join('');
  } else if (userCard) {
    userCard.style.display = 'none';
  }

  // Helper: save with current font/density/bgPattern from UI selects
  function _getOpts() {
    const opts = {};
    const fs = document.getElementById('theme-font-select');
    const ds = document.getElementById('theme-density-select');
    const ps = document.getElementById('theme-bg-pattern-select');
    const ec = document.getElementById('theme-bg-effect-color');
    const es = document.getElementById('theme-bg-intensity');
    const sz = document.getElementById('theme-bg-size');
    if (fs) opts.font = fs.value;
    if (ds) opts.density = ds.value;
    if (ps) opts.bgPattern = ps.value;
    if (ec) opts.bgEffectColor = ec.value;
    if (es) opts.bgEffectIntensity = parseFloat(es.value) / 100;
    if (sz) opts.bgEffectSize = parseFloat(sz.value) / 100;
    opts.bgEffectControls = _copyBackgroundEffectControlValues();
    const fr = document.getElementById('theme-frosted-toggle');
    if (fr) opts.frosted = !!fr.checked;
    return opts;
  }
  function _saveFull(name, colors) {
    const opts = _getOpts();
    save(name, colors, opts);
    // Keep the named custom-library entry identical to the active snapshot so
    // switching away and back loses nothing (T03).
    const ct = _loadCustomThemes();
    if (name && ct[name]) _writeCustomThemeEntry(name, colors, opts);
  }

  // Click handlers for all swatches (preset + custom) across both grids
  const allGrids = [grid, userGrid].filter(Boolean);
  allGrids.forEach(g => {
    g.querySelectorAll('.theme-swatch').forEach(sw => {
      sw.addEventListener('click', (e) => {
        if (e.target.closest('.theme-delete-btn')) return;
        const name = sw.dataset.theme;
        const colors = sw.dataset.custom ? customThemes[name] : THEMES[name];
        if (!colors) return;
        applyTheme(name, colors);
      });
    });
    g.querySelectorAll('.theme-delete-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const name = btn.dataset.delete;
        if (uiModule && uiModule.styledConfirm) {
          if (!await uiModule.styledConfirm(`Delete theme "${name}"?`, { confirmText: 'Delete', danger: true })) return;
        }
        deleteCustomTheme(name);
      });
    });
  });

  // Init color pickers from current theme and apply syntax colors
  const currentColors = THEMES[activeName] || customThemes[activeName] || (saved ? saved.colors : THEMES[DEFAULT_THEME]);
  applyTheme(activeName, currentColors, { persist: false, storedOptions: saved || {} });

  // Reference colors for per-picker reset (the theme you started from)
  const refName = saved ? saved.name : DEFAULT_THEME;
  const refColors = THEMES[refName] || customThemes[refName] || currentColors;
  const refDefaults = computeAdvancedDefaults(refColors);

  // Sync reset button visibility based on whether color differs from reference
  function syncResetButtons() {
    document.querySelectorAll('.color-reset-btn[data-reset]').forEach(btn => {
      const key = btn.dataset.reset;
      const picker = document.getElementById(pickerIds[key]);
      if (picker && refColors[key]) {
        btn.classList.toggle('changed', picker.value.toLowerCase() !== refColors[key].toLowerCase());
      }
    });
    document.querySelectorAll('.color-reset-btn[data-reset-adv]').forEach(btn => {
      const key = btn.dataset.resetAdv;
      const picker = document.getElementById('adv-' + key);
      const ref = refDefaults[key] || '';
      if (picker && ref) {
        btn.classList.toggle('changed', picker.value.toLowerCase() !== ref.toLowerCase());
      }
    });
  }

  // Color picker live updates.
  // NOTE: do NOT clone the input. attachColorPicker installed a value-getter
  // override + a mousedown handler on this exact element; cloning would orphan
  // both. Use a one-time bind flag instead.
  const pickerIds = { bg: 'clr-bg', fg: 'clr-fg', panel: 'clr-panel', border: 'clr-border', red: 'clr-red' };
  Object.entries(pickerIds).forEach(([key, id]) => {
    const el = document.getElementById(id);
    if (!el || el.dataset.themeBound === '1') return;
    el.dataset.themeBound = '1';
    el.addEventListener('input', () => {
      // Capture the OLD basic palette before we read the new picker values.
      // Used below to decide which advanced pickers carry a real user-set
      // override (value differs from the OLD computed default) vs. ones
      // that are just stale-default and should auto-refresh.
      const _oldColors = {};
      Object.entries(pickerIds).forEach(([k, pid]) => {
        // Picker value HAS already changed (input fired) for the one the
        // user touched. For that one, reading the current value gives the
        // NEW color, which is fine — _oldDefaults uses the rest. We use
        // computeAdvancedDefaults({...new}) once for the new defaults, and
        // the CSS variables for the OLD defaults.
      });
      const _rs = getComputedStyle(document.documentElement);
      _oldColors.bg     = (_rs.getPropertyValue('--bg')    || '').trim();
      _oldColors.fg     = (_rs.getPropertyValue('--fg')    || '').trim();
      _oldColors.panel  = (_rs.getPropertyValue('--panel') || '').trim();
      _oldColors.border = (_rs.getPropertyValue('--border')|| '').trim();
      _oldColors.red    = (_rs.getPropertyValue('--red')   || '').trim();
      const _oldDefaults = computeAdvancedDefaults(_oldColors);

      const colors = {};
      Object.entries(pickerIds).forEach(([k, pid]) => {
        colors[k] = document.getElementById(pid).value;
      });

      // Build the advanced override map: only pickers whose value differs
      // from the OLD default count as user-set. Untouched pickers (still
      // matching the old default) get auto-updated to the NEW default so
      // they keep tracking the basic palette (e.g. Send Btn follows Accent).
      const _newDefaults = computeAdvancedDefaults(colors);
      const _adv = {};
      let _hasAdv = false;
      // Normalize color strings to lowercase 6-char hex so getComputedStyle
      // values (which keep whatever was set — could be #abc, #ABCDEF, or
      // rgb()) compare correctly against color-input pickers (always
      // #rrggbb lowercase). Without this, every advanced picker reads as
      // "user-set" and we'd revert to the v161 bug.
      const _norm = (raw) => {
        let h = String(raw || '').trim().toLowerCase();
        if (!h) return '';
        // rgb(r,g,b) or rgba(r,g,b,a)
        const rgb = h.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
        if (rgb) {
          const hx = n => Math.max(0, Math.min(255, parseInt(n, 10))).toString(16).padStart(2, '0');
          return '#' + hx(rgb[1]) + hx(rgb[2]) + hx(rgb[3]);
        }
        if (h[0] !== '#') h = '#' + h;
        // Expand #rgb → #rrggbb
        if (/^#[0-9a-f]{3}$/.test(h)) {
          return '#' + h[1] + h[1] + h[2] + h[2] + h[3] + h[3];
        }
        return h;
      };
      for (const { key } of ADV_KEYS) {
        const pEl = document.getElementById('adv-' + key);
        if (!pEl) continue;
        if (_norm(pEl.value) !== _norm(_oldDefaults[key])) {
          _adv[key] = pEl.value;
          _hasAdv = true;
        } else {
          // Untouched — slide to the new default so it tracks the new palette.
          pEl.value = _newDefaults[key];
        }
      }
      if (_hasAdv) colors.advanced = _adv;
      applyColors(colors);
      // Commit the complete state transaction before any UI reinit so a
      // re-render can never reapply the previous stored snapshot (T02).
      // _saveFull also writes the named custom-library entry (T03).
      const _activeSaved = getSaved();
      const _activeName = _activeSaved && _activeSaved.name;
      const _customMap = _loadCustomThemes();
      if (_activeName && _customMap && _customMap[_activeName]) {
        _saveFull(_activeName, colors);
      } else {
        _saveFull('custom', colors);
      }
      _flashAutosaved();
      grid.querySelectorAll('.theme-swatch').forEach(s => s.classList.remove('active'));
      syncResetButtons();
    });
  });

  // Save custom theme — inline input
  const saveNameInputOld = document.getElementById('theme-save-name');
  const saveGoBtnOld = document.getElementById('theme-save-go');
  const saveError = document.getElementById('theme-save-error');
  if (saveGoBtnOld && saveNameInputOld) {
    const newGoBtn = saveGoBtnOld.cloneNode(true);
    saveGoBtnOld.parentNode.replaceChild(newGoBtn, saveGoBtnOld);
    const newNameInput = saveNameInputOld.cloneNode(true);
    saveNameInputOld.parentNode.replaceChild(newNameInput, saveNameInputOld);
    const doSave = () => {
      saveError.style.display = 'none';
      const name = newNameInput.value.trim();
      if (!name) { saveError.textContent = 'Enter a name.'; saveError.style.display = 'block'; return; }
      const slug = name.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '');
      if (!slug) { saveError.textContent = 'Invalid name.'; saveError.style.display = 'block'; return; }
      if (THEMES[slug]) { saveError.textContent = 'Cannot overwrite a built-in theme.'; saveError.style.display = 'block'; return; }
      const colors = {};
      const pickerIds2 = { bg: 'clr-bg', fg: 'clr-fg', panel: 'clr-panel', border: 'clr-border', red: 'clr-red' };
      Object.entries(pickerIds2).forEach(([k, pid]) => { colors[k] = document.getElementById(pid).value; });
      const adv = {};
      const defaults = computeAdvancedDefaults(colors);
      let hasAdv = false;
      for (const { key } of ADV_KEYS) {
        const el = document.getElementById('adv-' + key);
        if (el && el.value !== defaults[key]) { adv[key] = el.value; hasAdv = true; }
      }
      if (hasAdv) colors.advanced = adv;
      const opts = _getOpts();
      const result = saveCustomTheme(slug, colors, opts);
      if (result === 'limit') { saveError.textContent = 'Max ' + MAX_CUSTOM_THEMES + ' custom themes. Delete one first.'; saveError.style.display = 'block'; return; }
      save(slug, colors, opts);
      newNameInput.value = '';
      _flashAutosaved('Theme saved');
      uiModule.showToast?.('Theme saved');
      const prevHtml = newGoBtn.innerHTML;
      newGoBtn.disabled = true;
      newGoBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg><span>Saved</span>';
      setTimeout(() => {
        newGoBtn.disabled = false;
        newGoBtn.innerHTML = prevHtml;
      }, 1200);
    };
    newGoBtn.addEventListener('click', doSave);
    newNameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSave(); });
  }

  // Reset button
  const resetBtn = document.getElementById('theme-reset-btn');
  if (resetBtn) {
    const newReset = resetBtn.cloneNode(true);
    resetBtn.parentNode.replaceChild(newReset, resetBtn);
    newReset.addEventListener('click', () => {
      Storage.remove(_themeOwnerKey());
      if (!_themeOwner) Storage.remove(LS_KEY);
      const colors = THEMES[DEFAULT_THEME];
      // Persist through the same owner-bound save path so server hydration
      // cannot restore the old theme (T05).
      applyTheme(DEFAULT_THEME, colors, { persist: true });
    });
  }

  // Advanced section toggle
  const advToggle = document.getElementById('theme-adv-toggle');
  const advSection = document.getElementById('themeAdvanced');
  if (advToggle && advSection) {
    const newToggle = advToggle.cloneNode(true);
    advToggle.parentNode.replaceChild(newToggle, advToggle);
    newToggle.addEventListener('click', () => {
      advSection.classList.toggle('hidden');
      newToggle.classList.toggle('open');
      // Re-scan rows so advanced color inputs get the hover-highlight too.
      const root = document.getElementById('theme-tab-customize');
      if (root) root.dataset.zoneBound = '';
      initThemeZoneHighlight();
    });
  }
  // Wire hover-highlights on color rows so the user sees which UI zone
  // each input edits.
  initThemeZoneHighlight();

  // Advanced color picker live updates
  function readCurrentColors() {
    const pickerIds2 = { bg: 'clr-bg', fg: 'clr-fg', panel: 'clr-panel', border: 'clr-border', red: 'clr-red' };
    const c = {};
    Object.entries(pickerIds2).forEach(([k, pid]) => { c[k] = document.getElementById(pid).value; });
    return c;
  }

  function readAdvanced() {
    const adv = {};
    const base = readCurrentColors();
    const defaults = computeAdvancedDefaults(base);
    let hasOverrides = false;
    for (const { key } of ADV_KEYS) {
      const el = document.getElementById('adv-' + key);
      if (!el) continue;
      const v = (el.value || '').toLowerCase();
      // Skip empty or never-populated inputs so we don't accidentally store
      // them as overrides (and then write '#000000' to the CSS var).
      if (!v || !/^#[0-9a-f]{6}$/.test(v)) continue;
      if (v !== (defaults[key] || '').toLowerCase()) {
        adv[key] = el.value;
        hasOverrides = true;
      }
    }
    return hasOverrides ? adv : undefined;
  }

  for (const { key } of ADV_KEYS) {
    const el = document.getElementById('adv-' + key);
    if (!el || el.dataset.themeBound === '1') continue;
    el.dataset.themeBound = '1';
    el.addEventListener('input', () => {
      const base = readCurrentColors();
      base.advanced = readAdvanced();
      applyColors(base);
      // Commit before any UI reinit (T02); _saveFull writes the named
      // library entry with complete options (T03).
      const _activeSaved = getSaved();
      const _activeName = _activeSaved && _activeSaved.name;
      const _customMap = _loadCustomThemes();
      if (_activeName && _customMap && _customMap[_activeName]) {
        _saveFull(_activeName, base);
      } else {
        _saveFull('custom', base);
      }
      _flashAutosaved();
      grid.querySelectorAll('.theme-swatch').forEach(s => s.classList.remove('active'));
      syncResetButtons();
    });
  }

  // Clear advanced overrides button
  const advClearBtn = document.getElementById('theme-adv-clear');
  if (advClearBtn) {
    const newClear = advClearBtn.cloneNode(true);
    advClearBtn.parentNode.replaceChild(newClear, advClearBtn);
    newClear.addEventListener('click', () => {
      const base = readCurrentColors();
      delete base.advanced;
      applyColors(base);
      _saveFull('custom', base);
      syncAdvancedPickers(base);
      syncResetButtons();
    });
  }

  // Per-picker reset buttons (base colors)
  document.querySelectorAll('.color-reset-btn[data-reset]').forEach(btn => {
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.addEventListener('click', () => {
      const key = newBtn.dataset.reset;
      const picker = document.getElementById(pickerIds[key]);
      if (picker && refColors[key]) {
        picker.value = refColors[key];
        picker.dispatchEvent(new Event('input'));
      }
    });
  });

  // Effect color reset button
  document.querySelectorAll('.color-reset-btn[data-reset-effect]').forEach(btn => {
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.addEventListener('click', () => {
      const ec = document.getElementById('theme-bg-effect-color');
      if (ec) {
        const fg = currentColors.fg || '#9cdef2';
        ec.value = fg;
        applyBgEffectColor('');
        const s = getSaved(); if (s) _saveFull(s.name, s.colors);
      }
    });
  });

  // Per-picker reset buttons (advanced colors)
  document.querySelectorAll('.color-reset-btn[data-reset-adv]').forEach(btn => {
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
    newBtn.addEventListener('click', () => {
      const key = newBtn.dataset.resetAdv;
      const picker = document.getElementById('adv-' + key);
      if (picker) {
        picker.value = refDefaults[key] || computeAdvancedDefaults(refColors)[key];
        picker.dispatchEvent(new Event('input'));
      }
    });
  });

  // Initial sync of reset button visibility
  syncResetButtons();

  // Font, density, background pattern controls
  const _initialOptions = _getThemeOptions(activeName, saved || {});
  const _initFont = _initialOptions.font;
  const _initDensity = _initialOptions.density;
  const _initPattern = _initialOptions.bgPattern;
  const _initEffectColor = _initialOptions.bgEffectColor;
  const _initEffectIntensity = _initialOptions.bgEffectIntensity;
  const _initEffectSize = _initialOptions.bgEffectSize;
  const _initEffectControls = _initialOptions.bgEffectControls;
  const _initFrosted = _initialOptions.frosted;
  applyFontDensity(_initFont, _initDensity);
  applyBgEffectColor(_initEffectColor);
  applyBgEffectIntensity(_initEffectIntensity);
  applyBgEffectSize(_initEffectSize);
  applyBackgroundEffectControls(_initEffectControls);
  applyFrostedGlass(_initFrosted);

  const fontSelect = document.getElementById('theme-font-select');
  const densitySelect = document.getElementById('theme-density-select');
  const patternSelect = document.getElementById('theme-bg-pattern-select');

  if (fontSelect) {
    const nf = fontSelect.cloneNode(true); fontSelect.parentNode.replaceChild(nf, fontSelect);
    nf.value = _initFont;
    nf.addEventListener('change', () => {
      applyFontDensity(nf.value, document.getElementById('theme-density-select').value);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
    // Fetch custom fonts from local folder and populate dropdown
    fetch('/api/fonts/custom', { credentials: 'same-origin' })
      .then(r => r.json())
      .then(data => {
        _customFonts = data.fonts || {};
        const families = Object.keys(_customFonts);
        nf.querySelectorAll('option[data-custom-font]').forEach(o => o.remove());
        for (const fam of families) {
          const opt = document.createElement('option');
          opt.value = fam;
          opt.textContent = fam;
          opt.dataset.customFont = '1';
          nf.appendChild(opt);
        }
        // Restore saved value after options are populated, then reapply
        // the font so a late-loaded custom font is not left on fallback.
        // Density and backgrounds are untouched (T07).
        nf.value = _initFont;
        if (_customFonts[_initFont] && !FONT_MAP[_initFont]) {
          const densityEl = document.getElementById('theme-density-select');
          applyFontDensity(_initFont, densityEl ? densityEl.value : _initDensity);
        }
      })
      .catch(e => console.warn('Custom fonts fetch failed:', e));
  }
  if (densitySelect) {
    const nd = densitySelect.cloneNode(true); densitySelect.parentNode.replaceChild(nd, densitySelect);
    nd.value = _initDensity;
    nd.addEventListener('change', () => {
      applyFontDensity(document.getElementById('theme-font-select').value, nd.value);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }
  const textSizeSelect = document.getElementById('theme-text-size-select');
  if (textSizeSelect) {
    const nts = textSizeSelect.cloneNode(true); textSizeSelect.parentNode.replaceChild(nts, textSizeSelect);
    let initScale = DEFAULT_UI_SCALE;
    try { initScale = localStorage.getItem(UI_SCALE_KEY) || DEFAULT_UI_SCALE; } catch (e) {}
    nts.value = initScale;
    applyUiScale(initScale);
    nts.addEventListener('change', () => {
      applyUiScale(nts.value);
      try { localStorage.setItem(UI_SCALE_KEY, nts.value); } catch (e) {}
    });
  }
  if (patternSelect) {
    const np = patternSelect.cloneNode(true); patternSelect.parentNode.replaceChild(np, patternSelect);
    np.value = _initPattern;
    np.addEventListener('change', () => {
      applyBgPattern(np.value);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }

  const effectColorPicker = document.getElementById('theme-bg-effect-color');
  if (effectColorPicker && effectColorPicker.dataset.themeBound !== '1') {
    effectColorPicker.dataset.themeBound = '1';
    effectColorPicker.value = _initEffectColor || currentColors.fg || '#9cdef2';
    effectColorPicker.addEventListener('input', () => {
      applyBgEffectColor(effectColorPicker.value);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }

  const intensitySlider = document.getElementById('theme-bg-intensity');
  if (intensitySlider && intensitySlider.dataset.themeBound !== '1') {
    intensitySlider.dataset.themeBound = '1';
    intensitySlider.value = String(Math.round(_initEffectIntensity * 100));
    intensitySlider.addEventListener('input', () => {
      applyBgEffectIntensity(parseFloat(intensitySlider.value) / 100);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }

  const sizeSlider = document.getElementById('theme-bg-size');
  if (sizeSlider && sizeSlider.dataset.themeBound !== '1') {
    sizeSlider.dataset.themeBound = '1';
    sizeSlider.value = String(Math.round(_initEffectSize * 100));
    sizeSlider.addEventListener('input', () => {
      applyBgEffectSize(parseFloat(sizeSlider.value) / 100);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }

  const frostedToggle = document.getElementById('theme-frosted-toggle');
  if (frostedToggle && frostedToggle.dataset.themeBound !== '1') {
    frostedToggle.dataset.themeBound = '1';
    frostedToggle.checked = _initFrosted;
    frostedToggle.addEventListener('change', () => {
      applyFrostedGlass(frostedToggle.checked);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }

  // --- Color Harmony Generator (inside Advanced section) ---
  const harmonyGenBtnEl = document.getElementById('harmony-generate-btn');
  const harmonyAccentEl = document.getElementById('harmony-accent');
  // Make sure the in-house color picker really attached to this one. The
  // global initColorPickers() call earlier in initThemeUI should have grabbed
  // it, but in older sessions / partial loads it sometimes wasn't wrapped —
  // call attachColorPicker idempotently so the popover, suggestions, recents
  // and hex syncing all match every other color row.
  if (harmonyAccentEl) {
    try { attachColorPicker(harmonyAccentEl); } catch (_) {}
  }
  // Keep the hex display chip in sync with whatever the picker reports.
  const _harmonyHex = document.getElementById('harmony-accent-hex');
  if (harmonyAccentEl && _harmonyHex) {
    _harmonyHex.textContent = harmonyAccentEl.value || '#e06c75';
    harmonyAccentEl.addEventListener('input', () => {
      _harmonyHex.textContent = harmonyAccentEl.value;
    });
  }
  if (harmonyGenBtnEl) {
    const newGen = harmonyGenBtnEl.cloneNode(true);
    harmonyGenBtnEl.parentNode.replaceChild(newGen, harmonyGenBtnEl);
    newGen.addEventListener('click', () => {
      const accent = document.getElementById('harmony-accent').value;
      const type = document.getElementById('harmony-type').value;
      const mode = document.getElementById('harmony-mode').value;
      const colors = generateHarmonyColors(accent, type, mode);
      // Clear Clanker identity alongside the custom palette so no stale
      // identity class survives the transition (T06).
      applyThemeIdentity('custom');
      applyColors(colors);
      syncPickers(colors);
      _saveFull('custom', colors);
      grid.querySelectorAll('.theme-swatch').forEach(s => s.classList.remove('active'));
      const prev = document.getElementById('harmony-preview');
      if (prev) prev.innerHTML = [colors.bg, colors.panel, colors.fg, colors.border, colors.red].map(c => `<span style="background:${c}"></span>`).join('');
    });
  }
  if (harmonyAccentEl) {
    const newAcc = harmonyAccentEl.cloneNode(true);
    harmonyAccentEl.parentNode.replaceChild(newAcc, harmonyAccentEl);
    // Re-attach the in-house color picker to the fresh clone. cloneNode
    // copies the data-cp-attached="1" flag but NOT the listeners, so we
    // have to clear the flag first or attachColorPicker bails as a no-op.
    delete newAcc.dataset.cpAttached;
    newAcc.type = 'color'; // clone may have been type=text from prior attach
    try { attachColorPicker(newAcc); } catch (_) {}
    newAcc.addEventListener('input', () => {
      const type = document.getElementById('harmony-type').value;
      const mode = document.getElementById('harmony-mode').value;
      const colors = generateHarmonyColors(newAcc.value, type, mode);
      const prev = document.getElementById('harmony-preview');
      if (prev) prev.innerHTML = [colors.bg, colors.panel, colors.fg, colors.border, colors.red].map(c => `<span style="background:${c}"></span>`).join('');
      // Sync the hex chip beside the picker.
      const hex = document.getElementById('harmony-accent-hex');
      if (hex) hex.textContent = newAcc.value;
    });
  }

  // --- Import / Export ---
  const exportBtnEl = document.getElementById('theme-export-btn');
  const importBtnEl = document.getElementById('theme-import-btn');
  const importAreaEl = document.getElementById('theme-import-area');
  const importActionsEl = document.getElementById('theme-import-actions');
  const importGoEl = document.getElementById('theme-import-go');
  const importCancelEl = document.getElementById('theme-import-cancel');

  if (exportBtnEl) {
    const newExp = exportBtnEl.cloneNode(true);
    exportBtnEl.parentNode.replaceChild(newExp, exportBtnEl);
    newExp.addEventListener('click', () => {
      const colors = readCurrentColors();
      const adv = readAdvanced();
      if (adv) colors.advanced = adv;
      const cur = getSaved();
      const obj = { name: cur ? cur.name : 'custom', colors };
      if (cur && cur.font) obj.font = cur.font;
      if (cur && cur.density) obj.density = cur.density;
      if (cur && cur.bgPattern) obj.bgPattern = cur.bgPattern;
      if (cur && cur.bgEffectColor) obj.bgEffectColor = cur.bgEffectColor;
      if (cur && cur.bgEffectControls) obj.bgEffectControls = cur.bgEffectControls;
      if (cur && cur.bgEffectIntensity !== undefined) obj.bgEffectIntensity = cur.bgEffectIntensity;
      if (cur && cur.bgEffectSize !== undefined) obj.bgEffectSize = cur.bgEffectSize;
      if (cur && cur.frosted !== undefined) obj.frosted = cur.frosted;
      const json = JSON.stringify(obj, null, 2);
      const blob = new Blob([json], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'odysseus_' + (obj.name || 'theme') + '.json';
      a.click();
      URL.revokeObjectURL(url);
      newExp.innerHTML = '&#x2713; Downloaded!';
      setTimeout(() => { newExp.innerHTML = '&#x2913; Export'; }, 1500);
    });
  }

  if (importBtnEl && importAreaEl && importActionsEl) {
    const newImp = importBtnEl.cloneNode(true);
    importBtnEl.parentNode.replaceChild(newImp, importBtnEl);
    newImp.addEventListener('click', () => {
      importAreaEl.classList.toggle('hidden');
      importActionsEl.classList.toggle('hidden');
      importAreaEl.value = '';
      saveError.style.display = 'none';
    });
  }

  if (importGoEl && importAreaEl) {
    const newGo = importGoEl.cloneNode(true);
    importGoEl.parentNode.replaceChild(newGo, importGoEl);
    newGo.addEventListener('click', () => {
      saveError.style.display = 'none';
      let parsed;
      try { parsed = JSON.parse(importAreaEl.value.trim()); }
      catch { saveError.textContent = 'Invalid JSON.'; saveError.style.display = 'block'; return; }
      let colors = parsed.colors || parsed;
      const name = parsed.name || 'imported';
      const required = ['bg', 'fg', 'panel', 'border', 'red'];
      const missing = required.filter(k => !colors[k]);
      if (missing.length) { saveError.textContent = 'Missing: ' + missing.join(', '); saveError.style.display = 'block'; return; }
      const hexRe = /^#[0-9a-fA-F]{6}$/;
      for (const k of required) {
        if (!hexRe.test(colors[k])) { saveError.textContent = 'Bad hex for ' + k; saveError.style.display = 'block'; return; }
      }
      const colorData = { bg: colors.bg, fg: colors.fg, panel: colors.panel, border: colors.border, red: colors.red };
      if (colors.advanced && typeof colors.advanced === 'object') colorData.advanced = colors.advanced;
      const slug = name.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '') || 'imported';
      const opts = {};
      if (parsed.font) opts.font = parsed.font;
      if (parsed.density) opts.density = parsed.density;
      if (parsed.bgPattern) opts.bgPattern = parsed.bgPattern;
      if (parsed.bgEffectColor) opts.bgEffectColor = parsed.bgEffectColor;
      if (parsed.bgEffectControls && typeof parsed.bgEffectControls === 'object') opts.bgEffectControls = parsed.bgEffectControls;
      if (parsed.bgEffectIntensity !== undefined) opts.bgEffectIntensity = Number(parsed.bgEffectIntensity);
      if (parsed.bgEffectSize !== undefined) opts.bgEffectSize = Number(parsed.bgEffectSize);
      if (parsed.frosted !== undefined) opts.frosted = !!parsed.frosted;
      const result = saveCustomTheme(slug, colorData, opts);
      if (result === 'limit') { saveError.textContent = 'Max ' + MAX_CUSTOM_THEMES + ' custom themes. Delete one first.'; saveError.style.display = 'block'; return; }
      // Apply identity, colors and options together so no stale Clanker
      // identity class survives the transition (T06).
      applyTheme(slug, colorData, { persist: true, storedOptions: { ...colorData, ...opts } });
      importAreaEl.classList.add('hidden');
      importActionsEl.classList.add('hidden');
    });
  }

  if (importCancelEl && importAreaEl && importActionsEl) {
    const newCancel = importCancelEl.cloneNode(true);
    importCancelEl.parentNode.replaceChild(newCancel, importCancelEl);
    newCancel.addEventListener('click', () => {
      importAreaEl.classList.add('hidden');
      importActionsEl.classList.add('hidden');
      importAreaEl.value = '';
      saveError.style.display = 'none';
    });
  }

  // Theme popup now uses standard modal frame (not draggable)
}

// ── Zone highlighter ───────────────────────────────────────────────────
// Maps each color input id to a selector for the part of the UI it affects.
// When the user hovers the color row, we overlay a translucent box on the
// matching elements so it's obvious what's being edited.
const _THEME_ZONE_MAP = {
  'clr-bg':            'body',
  'clr-fg':            '.msg .body, .chat-input-bar',
  'clr-panel':         '.sidebar',
  'clr-border':        '.chat-input-bar, .sidebar, .msg .body',
  'clr-red':           '.send-btn, .icon-rail-btn.active',
  'theme-bg-effect-color': 'body',
  'adv-userBubbleBg':  '.msg.msg-user .body',
  'adv-aiBubbleBg':    '.msg.msg-ai .body',
  'adv-bubbleBorder':  '.msg .body',
  'adv-sidebarBg':     '.sidebar',
  'adv-sectionAccent': '.sidebar h4',
  'adv-brandColor':    '#sidebar-brand-btn',
  'adv-inputBg':       '#message',
  'adv-inputBorder':   '.chat-input-bar',
  'adv-sendBtnBg':     '.send-btn',
  'adv-sendBtnHover':  '.send-btn',
  'adv-codeBg':        'pre, code',
  'adv-codeFg':        'pre code, p code',
  'adv-toggleBg':      '.mode-toggle, .admin-switch',
  'adv-toggleActive':  '.mode-toggle-btn.active, .admin-switch input:checked + .admin-slider',
  'adv-accentPrimary': '.send-btn, .icon-rail-btn.active',
  'adv-accentError':   '.toast.error',
};

function _showThemeZoneHighlight(selector) {
  _clearThemeZoneHighlight();
  if (!selector) return;
  let els;
  try { els = document.querySelectorAll(selector); }
  catch { return; }
  els.forEach(el => {
    // Skip elements inside the theme modal — highlighting itself is noise.
    if (el.closest && el.closest('#theme-modal')) return;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    const overlay = document.createElement('div');
    overlay.className = 'theme-zone-highlight';
    overlay.style.top    = (r.top - 2) + 'px';
    overlay.style.left   = (r.left - 2) + 'px';
    overlay.style.width  = (r.width + 4) + 'px';
    overlay.style.height = (r.height + 4) + 'px';
    document.body.appendChild(overlay);
  });
}

function _clearThemeZoneHighlight() {
  document.querySelectorAll('.theme-zone-highlight').forEach(el => el.remove());
}

let _flashTimer = null;
function _flashAutosaved(label = 'Auto-saved') {
  let pill = document.getElementById('theme-autosaved-pill');
  if (!pill) {
    pill = document.createElement('div');
    pill.id = 'theme-autosaved-pill';
    pill.className = 'theme-autosaved-pill';
    pill.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg><span></span>';
    // Anchor inside the customize tab so it floats with the form.
    const customizeTab = document.getElementById('theme-tab-customize');
    (customizeTab || document.body).appendChild(pill);
  }
  const labelEl = pill.querySelector('span');
  if (labelEl) labelEl.textContent = label;
  pill.classList.add('visible');
  clearTimeout(_flashTimer);
  _flashTimer = setTimeout(() => pill.classList.remove('visible'), 1100);
}

// Wire hover-to-highlight on every color row inside the theme modal. Call
// once after the modal markup is in the DOM. Idempotent.
export function initThemeZoneHighlight() {
  const root = document.getElementById('theme-tab-customize');
  if (!root || root.dataset.zoneBound === '1') return;
  root.dataset.zoneBound = '1';
  root.querySelectorAll('.color-row').forEach(row => {
    const input = row.querySelector('input[type="color"]');
    if (!input) return;
    const sel = _THEME_ZONE_MAP[input.id];
    if (!sel) return;
    row.addEventListener('mouseenter', () => _showThemeZoneHighlight(sel));
    row.addEventListener('mouseleave', _clearThemeZoneHighlight);
    // Also trigger when the picker actually opens (input focus)
    input.addEventListener('focus', () => _showThemeZoneHighlight(sel));
    input.addEventListener('blur', _clearThemeZoneHighlight);
  });
  // Clear highlight when the modal closes.
  const modal = document.getElementById('theme-modal');
  if (modal) {
    new MutationObserver(() => {
      if (modal.classList.contains('hidden')) _clearThemeZoneHighlight();
    }).observe(modal, { attributes: true, attributeFilter: ['class'] });
  }
}

// Generic draggable helper for fixed-position elements
// Thin wrapper around the shared makeWindowDraggable helper. Existing
// callers pass (el, handle) — `el` is what gets moved, `handle` is the
// drag handle. No fullscreen support (none of these consumers wanted it).
export function makeDraggable(el, handle) {
  if (!el || !handle) return;
  const dockTarget = (el.closest && el.closest('.modal')) || el;
  const dragOptions = {
    content: el,
    header: handle,
    // Don't start a window-drag when the user grabs an interactive control
    // in the header — e.g. the theme opacity slider now lives next to the
    // title, and dragging its thumb must move the slider, not the window.
    skipSelector: 'button, input, select, .theme-opacity-wrap',
  };
  if (dockTarget && dockTarget.id === 'theme-modal') {
    dragOptions.onEnterFullscreen = () => {
      snapModalToZone(dockTarget, {
        name: 'fullscreen',
        rect: {
          left: 0,
          top: 0,
          width: window.innerWidth || document.documentElement.clientWidth || 0,
          height: window.innerHeight || document.documentElement.clientHeight || 0,
        },
      });
    };
  }
  makeWindowDraggable(dockTarget, dragOptions);
}

// Toggle the popup
export function togglePopup() {
  const modal = document.getElementById('theme-modal');
  if (!modal) return;
  if (document.getElementById('settings-theme-controls')?.contains(document.getElementById('theme-popup'))) {
    if (window.settingsModule?.open) {
      if (document.getElementById('settings-modal')?.classList.contains('hidden')) {
        window.settingsModule.open('appearance');
      } else {
        window.settingsModule.close?.();
      }
      return;
    }
  }
  const visible = !modal.classList.contains('hidden');
  if (visible) {
    modal.classList.add('hidden');
  } else {
    modal.classList.remove('hidden');
  }
}

export function closePopup() {
  const modal = document.getElementById('theme-modal');
  if (!modal) return;
  if (document.getElementById('settings-theme-controls')?.contains(document.getElementById('theme-popup'))) {
    window.settingsModule?.close?.();
    return;
  }
  const content = modal.querySelector('.modal-content');
  if (content && !content.classList.contains('modal-closing')) {
    content.classList.add('modal-closing');
    content.addEventListener('animationend', () => {
      modal.classList.add('hidden');
      content.classList.remove('modal-closing');
    }, { once: true });
    setTimeout(() => { if (!modal.classList.contains('hidden')) { modal.classList.add('hidden'); content.classList.remove('modal-closing'); } }, 250);
  } else {
    modal.classList.add('hidden');
  }
}

// Expose for app.js wiring + AI ui_control
export function getCustomThemes() { return _loadCustomThemes(); }

function _readClankerEffectConfig(fullPalette = false) {
  const styles = getComputedStyle(document.body);
  const color = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
  const rawIntensity = parseFloat(styles.getPropertyValue('--bg-effect-intensity'));
  const effectColor = color('--bg-effect-color', color('--fg', '#62C7E8'));
  const clankerColors = [
    effectColor,
    color('--clanker-gold', '#F6BE48'),
    color('--clanker-lime', '#A8DE53'),
    color('--clanker-pink', '#ED6AB0'),
    color('--clanker-coral', '#FF776E'),
    color('--clanker-lilac', '#B7A7E8'),
  ];
  return {
    intensity: Number.isFinite(rawIntensity) ? Math.max(0, Math.min(1, rawIntensity)) : 0.64,
    size: _getEffectSize(),
    outline: color('--clanker-outline', '#0E0F12'),
    colors: (fullPalette || _clankerPaletteEnabled) ? clankerColors : clankerColors.map(() => effectColor),
  };
}

function _runBackgroundCanvas({ canvas, bodyClass, resize, paint, resizeTarget = null, onLayoutChange = null, onDispose = null }) {
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let animationFrame = 0;
  let resizeFrame = 0;
  let resizeTimer = 0;
  let animationTime = 0;
  let previousFrame = 0;
  let viewportKey = '';
  let resizeObserver = null;
  let disposed = false;

  function cancelFrame() {
    if (!animationFrame) return;
    window.cancelAnimationFrame(animationFrame);
    animationFrame = 0;
  }

  function scheduleFrame() {
    if (!disposed && !motion.matches && !animationFrame) {
      animationFrame = window.requestAnimationFrame(frame);
    }
  }

  function resizeIfNeeded(force = false) {
    const nextViewportKey = `${window.innerWidth}:${window.innerHeight}:${window.devicePixelRatio || 1}`;
    if (!force && viewportKey === nextViewportKey) return false;
    viewportKey = nextViewportKey;
    resize();
    return true;
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    cancelFrame();
    window.cancelAnimationFrame(resizeFrame);
    window.clearTimeout(resizeTimer);
    resizeFrame = 0;
    resizeTimer = 0;
    window.removeEventListener('resize', handleResize);
    if (resizeObserver) resizeObserver.disconnect();
    resizeObserver = null;
    if (motion.removeEventListener) motion.removeEventListener('change', handleMotionChange);
    else if (motion.removeListener) motion.removeListener(handleMotionChange);
    document.removeEventListener('visibilitychange', handleVisibilityChange);
    if (_activeBackgroundEffectDispose === dispose) _activeBackgroundEffectDispose = null;
    if (window[_BACKGROUND_OWNER_KEY] === dispose) window[_BACKGROUND_OWNER_KEY] = null;
    // Release scene-owned raster caches and paint on unmount (T11 / C8).
    const activeScene = canvas.__backgroundScene;
    if (activeScene) {
      if (activeScene.emojiSprites && typeof activeScene.emojiSprites.clear === 'function') activeScene.emojiSprites.clear();
      if (activeScene.paintField && typeof activeScene.paintField.clear === 'function') activeScene.paintField.clear();
    }
    canvas.__backgroundScene = null;
    if (onDispose) onDispose();
    canvas.remove();
  }

  function frame(time = 0) {
    animationFrame = 0;
    if (disposed) return;
    if (!canvas.isConnected || !document.body.classList.contains(bodyClass)) {
      dispose();
      return;
    }
    if (document.hidden) {
      animationFrame = 0;
      return;
    }
    if (!motion.matches && previousFrame) animationTime += Math.min(Math.max(time - previousFrame, 0), 34);
    previousFrame = time;
    paint(motion.matches ? 0 : animationTime, motion.matches);
    scheduleFrame();
  }

  function handleResize() {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      resizeTimer = 0;
      if (disposed || resizeFrame) return;
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = 0;
        if (!resizeIfNeeded() || disposed) return;
        // A canvas resize clears its bitmap. Repaint in this callback so the
        // compositor never receives a cleared canvas while its RAF is pending.
        paint(motion.matches ? 0 : animationTime, motion.matches);
      });
    }, 96);
  }

  function handleResizeTarget() {
    if (onLayoutChange) onLayoutChange();
  }

  function handleMotionChange() {
    cancelFrame();
    previousFrame = 0;
    frame(performance.now());
  }

  function handleVisibilityChange() {
    cancelFrame();
    previousFrame = 0;
    if (document.hidden) {
      animationFrame = 0;
      return;
    }
    frame(performance.now());
  }

  const activeDispose = window[_BACKGROUND_OWNER_KEY] || _activeBackgroundEffectDispose;
  if (activeDispose) activeDispose();
  _activeBackgroundEffectDispose = dispose;
  window[_BACKGROUND_OWNER_KEY] = dispose;
  canvas.dataset.backgroundEffectCanvas = 'true';
  canvas.__disposeEffect = dispose;
  window.addEventListener('resize', handleResize);
  if (resizeTarget && typeof ResizeObserver === 'function') {
    resizeObserver = new ResizeObserver(handleResizeTarget);
    resizeObserver.observe(resizeTarget);
  }
  if (motion.addEventListener) motion.addEventListener('change', handleMotionChange);
  else if (motion.addListener) motion.addListener(handleMotionChange);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  resizeIfNeeded(true);
  frame(performance.now());

  // T10: one-shot paint for reduced-motion (and any dirty-style) invalidation.
  canvas.__requestBackgroundRepaint = () => {
    if (!disposed) frame(performance.now());
  };
}

function _clankerSafeBounds(width, height, size) {
  const shortest = Math.max(1, Math.min(width, height));
  const inset = Math.max(12, Math.min(shortest * .14, 34 * Math.max(.5, size)));
  return {
    inset,
    left: inset,
    top: inset,
    right: Math.max(inset, width - inset),
    bottom: Math.max(inset, height - inset),
    width: Math.max(1, width - inset * 2),
    height: Math.max(1, height - inset * 2),
  };
}

function _clankerEdgeAlpha(x, y, extent, bounds) {
  const distance = Math.min(
    x - bounds.left,
    bounds.right - x,
    y - bounds.top,
    bounds.bottom - y,
  );
  const fadeBand = Math.max(8, Math.min(48, extent * .75));
  return Math.max(0, Math.min(1, (distance - extent) / fadeBand));
}

function _mountClankerEffect({ id, bodyClass, build, draw, getSceneKey = null }) {
  if (document.getElementById(id)) return;
  const host = document.getElementById('chat-container') || document.body;
  const chatPane = host.id === 'chat-container';
  const canvas = document.createElement('canvas');
  canvas.id = id;
  canvas.style.cssText = chatPane
    ? 'position:absolute;left:0;top:0;pointer-events:none;z-index:-1;'
    : 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  canvas.setAttribute('aria-hidden', 'true');
  if (chatPane) host.classList.add('background-effect-host');
  host.prepend(canvas);

  // Match the working built-in effects: one synchronized visible Canvas2D,
  // cleared and painted once inside the shared requestAnimationFrame callback.
  const ctx = canvas.getContext('2d');
  if (!ctx) { canvas.remove(); return; }
  let width = 0;
  let height = 0;
  let canvasWidth = 0;
  let canvasHeight = 0;
  let dpr = 1;
  let scene = null;
  let sceneKey = '';

  function updateCanvasLayout() {
    if (!chatPane) return;
    const rect = host.getBoundingClientRect();
    canvas.style.left = `${-Math.round(rect.left)}px`;
    canvas.style.top = `${-Math.round(rect.top)}px`;
    canvas.style.width = `${window.innerWidth}px`;
    canvas.style.height = `${window.innerHeight}px`;
  }

  function resize() {
    const rect = host.getBoundingClientRect();
    const nextWidth = chatPane ? Math.max(1, window.innerWidth) : Math.max(1, Math.round(rect.width));
    const nextHeight = chatPane ? Math.max(1, window.innerHeight) : Math.max(1, Math.round(rect.height));
    const nextDpr = Math.min(window.devicePixelRatio || 1, 2);
    const geometryChanged = width !== nextWidth || height !== nextHeight || dpr !== nextDpr;
    width = canvasWidth = nextWidth;
    height = canvasHeight = nextHeight;
    dpr = nextDpr;
    updateCanvasLayout();
    if (geometryChanged) {
      canvas.width = Math.max(1, Math.floor(canvasWidth * dpr));
      canvas.height = Math.max(1, Math.floor(canvasHeight * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      sceneKey = '';
    }
  }

  function paint(time, reduced) {
    // Clanker-native canvases own their full palette. Tying it to a body class
    // on every frame created a second, monochrome render state.
    const config = _readClankerEffectConfig(true);
    // Scene geometry follows the vanilla lifecycle: rebuild only when geometry
    // or effect-control topology changes. Palette/intensity reads stay live in
    // draw() so style-only updates preserve scene identity and trajectories
    // (T08). The chat pane is already the clipping boundary. Keep its artwork
    // full bleed so a viewport-scale crop cannot leave a gutter on one edge.
    const safeBounds = chatPane
      ? { inset: 0, left: 0, top: 0, right: width, bottom: height, width, height }
      : _clankerSafeBounds(width, height, config.size);
    const controlSceneKey = typeof getSceneKey === 'function' ? getSceneKey() : '';
    const nextSceneKey = `${width}:${height}:${config.size}:${safeBounds.inset}:${controlSceneKey}`;
    if (sceneKey !== nextSceneKey) {
      scene = build({ width, height, safeBounds, dpr, ...config });
      canvas.__backgroundStaticCanvas = scene?.staticCanvas || null;
      sceneKey = nextSceneKey;
    }
    // Test and diagnostics seam: expose the current immutable geometry plus
    // mutable animation state without creating a second owner or render loop.
    canvas.__backgroundScene = scene;
    ctx.clearRect(0, 0, canvasWidth, canvasHeight);
    ctx.save();
    draw(ctx, {
      width,
      height,
      time,
      reduced,
      scene,
      safeBounds,
      ...config,
    });
    ctx.restore();
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    ctx.setLineDash([]);
  }

  // Diagnostics seam used by the isolated Kene acceptance fixture. It invokes
  // the same scene build/read/draw path as the RAF callback without creating a
  // second owner or loop; production callers never need this property.
  canvas.__backgroundPaint = paint;

  _runBackgroundCanvas({
    canvas,
    bodyClass,
    resize,
    paint,
    resizeTarget: chatPane ? host : null,
    onLayoutChange: chatPane ? updateCanvasLayout : null,
    onDispose: chatPane ? () => host.classList.remove('background-effect-host') : null,
  });
}

function _pointOnQuadratic(route, t) {
  const inv = 1 - t;
  return {
    x: inv * inv * route.a.x + 2 * inv * t * route.cx + t * t * route.b.x,
    y: inv * inv * route.a.y + 2 * inv * t * route.cy + t * t * route.b.y,
  };
}

function _pointOnPolyline(points, progress) {
  if (!points.length) return { x: 0, y: 0 };
  const lengths = [];
  let total = 0;
  for (let index = 1; index < points.length; index += 1) {
    const length = Math.hypot(points[index].x - points[index - 1].x, points[index].y - points[index - 1].y);
    lengths.push(length);
    total += length;
  }
  let target = ((progress % 1) + 1) % 1 * total;
  for (let index = 0; index < lengths.length; index += 1) {
    if (target <= lengths[index]) {
      const amount = lengths[index] ? target / lengths[index] : 0;
      return {
        x: points[index].x + (points[index + 1].x - points[index].x) * amount,
        y: points[index].y + (points[index + 1].y - points[index].y) * amount,
      };
    }
    target -= lengths[index];
  }
  return points[points.length - 1];
}

function _pointOnCachedPolyline(route, progress) {
  if (!route.points.length) return { x: 0, y: 0 };
  const target = ((progress % 1) + 1) % 1 * route.total;
  // Find the segment containing the target without allowing the lower bound
  // to stall when the target falls before the first cumulative distance.
  let low = 0;
  let high = route.cumulative.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high + 1) / 2);
    if (route.cumulative[middle] <= target) low = middle;
    else high = middle - 1;
  }
  const segment = Math.min(low, route.points.length - 2);
  const start = route.cumulative[segment];
  const length = route.cumulative[segment + 1] - start;
  const amount = length ? (target - start) / length : 0;
  const a = route.points[segment];
  const b = route.points[segment + 1];
  return {
    x: a.x + (b.x - a.x) * amount,
    y: a.y + (b.y - a.y) * amount,
  };
}

// S25 Signal Routes — persistent bead state on a bounded path graph.
//
// The previous renderer wrapped each packet's progress with `% 1`, so a packet
// that reached a route endpoint teleported back to the start. Beads here own a
// distance along one edge and roll into a connected edge at the junction, so
// position is continuous through the graph. A fixed pool, branch cooldown and
// retirement keep density stable over long runs; palette/intensity changes do
// not rebuild the scene (see `_mountClankerEffect` scene key).
function _initClankerRoutefield() {
  // Pool and branch policy. `MAX_ROUTE_BEADS` is a hard cap; splits refuse to
  // spawn above it and old beads retire when a younger one needs the slot.
  const MAX_ROUTE_BEADS = 48;
  const BEAD_LIFETIME_MS = 42000;
  const BRANCH_COOLDOWN_MS = 5200;
  // Roll weights for the seeded per-frame bead policy. The effective per-frame
  // direction-change chance is WEIGHT * .02 (≈ 0.0032) — the name says weight,
  // not chance, so the scale at the use site is not a surprise.
  const DIRECTION_CHANGE_WEIGHT = .16;
  const COLOR_CHANGE_CHANCE = .12;
  const SPLIT_CHANCE = .10;
  // Sampled polyline segments per quadratic edge — enough for smooth travel
  // without paying a bezier evaluation per bead per frame.
  const SAMPLES_PER_ROUTE = 18;

  const sampleQuadratic = (route) => {
    const points = [];
    for (let index = 0; index <= SAMPLES_PER_ROUTE; index += 1) {
      points.push(_pointOnQuadratic(route, index / SAMPLES_PER_ROUTE));
    }
    const cumulative = [0];
    for (let index = 1; index < points.length; index += 1) {
      cumulative.push(cumulative[index - 1] + Math.hypot(
        points[index].x - points[index - 1].x,
        points[index].y - points[index - 1].y,
      ));
    }
    return { points, cumulative, total: cumulative[cumulative.length - 1] || 1 };
  };

  const beadPosition = (route, distance) => {
    const samples = route.samples;
    const clamped = Math.max(0, Math.min(samples.total, distance));
    let low = 0;
    let high = samples.cumulative.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high + 1) / 2);
      if (samples.cumulative[middle] <= clamped) low = middle;
      else high = middle - 1;
    }
    const segment = Math.min(low, samples.points.length - 2);
    const start = samples.cumulative[segment];
    const length = samples.cumulative[segment + 1] - start;
    const amount = length ? (clamped - start) / length : 0;
    const a = samples.points[segment];
    const b = samples.points[segment + 1];
    return { x: a.x + (b.x - a.x) * amount, y: a.y + (b.y - a.y) * amount };
  };

  _mountClankerEffect({
    id: 'clanker-routefield-canvas',
    bodyClass: 'bg-pattern-clanker-routefield',
    build: ({ size, safeBounds }) => {
      const columns = Math.max(6, Math.ceil(safeBounds.width / (210 * size)) + 1);
      const rows = Math.max(5, Math.ceil(safeBounds.height / (165 * size)) + 1);
      const gapX = safeBounds.width / (columns - 1);
      const gapY = safeBounds.height / (rows - 1);
      const nodes = [];
      for (let row = 0; row < rows; row += 1) {
        for (let column = 0; column < columns; column += 1) {
          const seed = row * 97 + column * 29;
          const baseX = safeBounds.left + column * gapX;
          const baseY = safeBounds.top + row * gapY;
          nodes.push({
            x: column === 0 ? safeBounds.left : column === columns - 1 ? safeBounds.right : baseX + (_clankerNoise(seed) - .5) * gapX * .38,
            y: row === 0 ? safeBounds.top : row === rows - 1 ? safeBounds.bottom : baseY + (_clankerNoise(seed + 13) - .5) * gapY * .34,
            color: (row * 2 + column) % 6,
            hub: (row + column * 2) % 6 === 0,
            // Branch cooldown clock (ms) — a junction that just split stays
            // quiet so one hot node cannot monopolize the pool.
            lastBranchAt: -BRANCH_COOLDOWN_MS,
            incident: [],
          });
        }
      }
      const routes = [];
      const nodeAt = (row, column) => nodes[row * columns + column];
      const connect = (a, b, seed) => {
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const length = Math.hypot(dx, dy) || 1;
        const bend = (_clankerNoise(seed) - .5) * Math.min(gapX, gapY) * .7;
        const route = {
          a, b,
          index: routes.length,
          phase: _clankerNoise(seed + 41),
          color: (a.color + b.color + seed) % 6,
          cx: Math.max(safeBounds.left, Math.min(safeBounds.right, (a.x + b.x) / 2 - (dy / length) * bend)),
          cy: Math.max(safeBounds.top, Math.min(safeBounds.bottom, (a.y + b.y) / 2 + (dx / length) * bend)),
        };
        route.samples = sampleQuadratic(route);
        routes.push(route);
        a.incident.push(route.index);
        b.incident.push(route.index);
      };
      for (let row = 0; row < rows; row += 1) {
        for (let column = 0; column < columns; column += 1) {
          const seed = row * 101 + column * 17;
          if (column < columns - 1) connect(nodeAt(row, column), nodeAt(row, column + 1), seed);
          if (row < rows - 1 && (row + column) % 2 === 0) connect(nodeAt(row, column), nodeAt(row + 1, column), seed + 7);
          if (row < rows - 1 && column < columns - 1 && (row * 3 + column) % 5 === 0) {
            connect(nodeAt(row, column), nodeAt(row + 1, column + 1), seed + 19);
          }
        }
      }

      // Persistent bead pool. Each bead owns one edge and a distance along it.
      // Positions survive palette/intensity changes because the scene is not
      // rebuilt for style (T08) — only for geometry/topology.
      const beads = [];
      const seedBeads = Math.min(22, Math.max(10, Math.round(routes.length * .28)));
      for (let index = 0; index < seedBeads && routes.length; index += 1) {
        const routeIndex = Math.floor(_clankerNoise(index * 53 + 3) * routes.length);
        const route = routes[Math.min(routeIndex, routes.length - 1)];
        beads.push({
          routeIndex: route.index,
          distance: _clankerNoise(index * 29 + 7) * route.samples.total,
          direction: _clankerNoise(index * 19 + 11) > .5 ? 1 : -1,
          color: route.color,
          speed: 34 + _clankerNoise(index * 41 + 13) * 26,
          glow: .55 + _clankerNoise(index * 61 + 17) * .45,
          bornAt: -_clankerNoise(index * 71 + 19) * BEAD_LIFETIME_MS,
          // Last time this bead asked for a split — feeds the junction clock.
          lastSplitAt: -BRANCH_COOLDOWN_MS,
        });
      }

      return {
        nodes,
        hubs: nodes.filter(node => node.hub),
        routes,
        beads,
        beadPolicy: {
          MAX_ROUTE_BEADS, BEAD_LIFETIME_MS, BRANCH_COOLDOWN_MS,
          DIRECTION_CHANGE_WEIGHT, COLOR_CHANGE_CHANCE, SPLIT_CHANCE,
        },
        stats: { splits: 0, retirements: 0, transitions: 0 },
        lastTime: 0,
      };
    },
    draw: (ctx, { time, reduced, scene, intensity, size, colors, outline }) => {
      // Reduced motion is a hard visual freeze: geometry stays visible and
      // beads are pinned to their seeded positions so a delayed repaint cannot
      // move the scene. No continuous travel, no junction rolls.
      const renderTime = reduced ? 0 : time;
      const elapsed = (!reduced && scene.lastTime && renderTime > scene.lastTime)
        ? Math.min(64, renderTime - scene.lastTime) : 0;
      if (!reduced) scene.lastTime = renderTime;

      for (const node of scene.hubs) {
        ctx.save();
        ctx.translate(node.x, node.y);
        ctx.rotate(-.28);
        ctx.fillStyle = outline;
        ctx.globalAlpha = intensity * .72;
        ctx.fillRect(-18 * size, -6 * size, 36 * size, 12 * size);
        for (let segment = 0; segment < 3; segment += 1) {
          ctx.fillStyle = colors[(node.color + segment) % colors.length];
          ctx.globalAlpha = intensity * (.5 + segment * .1);
          ctx.fillRect((-15 + segment * 11) * size, -3 * size, 8 * size, 6 * size);
        }
        ctx.restore();
      }
      for (const route of scene.routes) {
        ctx.beginPath();
        ctx.moveTo(route.a.x, route.a.y);
        ctx.quadraticCurveTo(route.cx, route.cy, route.b.x, route.b.y);
        ctx.setLineDash([]);
        ctx.strokeStyle = outline;
        ctx.lineWidth = 4 * size;
        ctx.globalAlpha = intensity * .62;
        ctx.stroke();
        ctx.strokeStyle = colors[route.color];
        ctx.lineWidth = 1.1 * size;
        ctx.globalAlpha = intensity * .22;
        ctx.stroke();
        ctx.setLineDash([3 * size, 12 * size]);
        ctx.lineDashOffset = 0;
        ctx.strokeStyle = colors[route.color];
        ctx.lineWidth = 1.55 * size;
        ctx.globalAlpha = intensity * .64;
        ctx.stroke();
      }
      ctx.setLineDash([]);
      for (const node of scene.nodes) {
        const radius = (node.hub ? 5 : 3.5) * size;
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + 2 * size, 0, Math.PI * 2);
        ctx.fillStyle = outline;
        ctx.globalAlpha = intensity * 0.82;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = colors[node.color];
        ctx.globalAlpha = intensity * (node.hub ? 0.88 : 0.62);
        ctx.fill();
      }

      // ── Beads ──
      const policy = scene.beadPolicy;
      const advance = (bead) => {
        if (!elapsed || !scene.routes.length) return;
        const route = scene.routes[bead.routeIndex];
        if (!route) return;
        const now = renderTime;
        // Lifetime: retire and respawn on a random edge so density stays
        // stable without letting the pool grow.
        if (now - bead.bornAt > policy.BEAD_LIFETIME_MS) {
          const respawnIndex = Math.floor(_clankerNoise(now * .001 + bead.routeIndex * 7) * scene.routes.length);
          const respawn = scene.routes[Math.min(respawnIndex, scene.routes.length - 1)];
          bead.routeIndex = respawn.index;
          bead.distance = _clankerNoise(now * .0013 + respawn.index) * respawn.samples.total;
          bead.direction = _clankerNoise(now * .0017 + respawn.index) > .5 ? 1 : -1;
          bead.color = respawn.color;
          bead.bornAt = now;
          bead.glow = .55 + _clankerNoise(now * .0019) * .45;
          scene.stats.retirements += 1;
          return;
        }
        bead.distance += bead.speed * (size || 1) * .06 * elapsed * bead.direction;

        // Occasional direction / color changes — seeded, not per-frame random.
        const roll = _clankerNoise(now * .0007 + bead.routeIndex * 13 + bead.distance * .001);
        if (roll < policy.DIRECTION_CHANGE_WEIGHT * .02) bead.direction *= -1;
        else if (roll < (policy.DIRECTION_CHANGE_WEIGHT + policy.COLOR_CHANGE_CHANCE) * .02) {
          bead.color = (bead.color + 1) % colors.length;
        }

        const total = route.samples.total;
        if (bead.distance <= 0 || bead.distance >= total) {
          // Reached a junction. Roll into a connected edge starting at the
          // arrival node — the bead's position stays exactly on the junction.
          const arrivingAt = bead.direction > 0 ? route.b : route.a;
          const leavingFrom = bead.direction > 0 ? route.a : route.b;
          const candidates = arrivingAt.incident
            .map(index => scene.routes[index])
            .filter(candidate => candidate && candidate !== route);
          if (!candidates.length) {
            bead.direction *= -1;
            bead.distance = Math.max(0, Math.min(total, bead.distance));
            return;
          }
          // Prefer an edge that does not immediately reverse onto the node we
          // just left, so travel reads as continuous flow through the junction.
          const forward = candidates.filter(candidate =>
            candidate.a !== leavingFrom && candidate.b !== leavingFrom);
          const pool = forward.length ? forward : candidates;
          const choice = Math.floor(_clankerNoise(now * .0009 + bead.routeIndex * 17) * pool.length);
          const next = pool[Math.min(choice, pool.length - 1)];
          bead.routeIndex = next.index;
          // Continuous handoff: enter the new edge at the shared junction.
          const enterFromB = next.a === arrivingAt;
          bead.direction = enterFromB ? -1 : 1;
          bead.distance = enterFromB ? next.samples.total : 0;
          if (_clankerNoise(now * .0011 + next.index) < policy.COLOR_CHANGE_CHANCE * .2) {
            bead.color = next.color;
          }
          scene.stats.transitions += 1;

          // Splits: at a cooled-down junction, with pool headroom, a second
          // bead peels onto a different incident edge.
          const cooled = now - arrivingAt.lastBranchAt >= policy.BRANCH_COOLDOWN_MS;
          const wantsSplit = _clankerNoise(now * .0021 + next.index * 3) < policy.SPLIT_CHANCE * .08;
          if (cooled && wantsSplit && scene.beads.length < policy.MAX_ROUTE_BEADS) {
            const alternate = candidates.filter(candidate => candidate !== next);
            if (alternate.length) {
              const alt = alternate[Math.floor(_clankerNoise(now * .0023 + next.index) * alternate.length)];
              const altFromB = alt.a === arrivingAt;
              scene.beads.push({
                routeIndex: alt.index,
                distance: altFromB ? alt.samples.total : 0,
                direction: altFromB ? -1 : 1,
                color: alt.color,
                speed: bead.speed * (0.85 + _clankerNoise(now * .0027) * .3),
                glow: .55 + _clankerNoise(now * .0029) * .45,
                bornAt: now,
                lastSplitAt: now,
              });
              arrivingAt.lastBranchAt = now;
              bead.lastSplitAt = now;
              scene.stats.splits += 1;
            }
          }
        }
      };

      if (!reduced) {
        for (const bead of scene.beads) advance(bead);
        // Hard cap enforcement: if a burst pushed past the pool, retire the
        // oldest beads. Density must never grow without bound.
        while (scene.beads.length > policy.MAX_ROUTE_BEADS) {
          scene.beads.shift();
          scene.stats.retirements += 1;
        }
      }

      for (const bead of scene.beads) {
        const route = scene.routes[bead.routeIndex];
        if (!route) continue;
        const point = beadPosition(route, bead.distance);
        const radius = (2.6 + bead.glow * 1.2) * size;
        // Short local glow only — a soft halo, never a snake body or trail.
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius + 2.4 * size, 0, Math.PI * 2);
        ctx.fillStyle = colors[bead.color];
        ctx.globalAlpha = intensity * .16 * bead.glow;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = outline;
        ctx.globalAlpha = intensity * .85;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius * .68, 0, Math.PI * 2);
        ctx.fillStyle = colors[bead.color];
        ctx.globalAlpha = intensity;
        ctx.fill();
      }
    },
  });
}

// Fixed real-time fade-out for Signal Weave snakes: every snake fades out over
// the same wall-clock interval regardless of its per-snake lifetime/duration
// (which the lifetime/length controls still freely adjust). Fade-in stays
// phase-relative — unchanged. Derived from the default visual baseline
// (16000ms baseDuration / 7 ≈ 2286ms), rounded; clamped to half the cycle so a
// pathologically short lifetime still reaches full visibility before fading.
const SNAKE_FADE_OUT_MS = 2200;
const SNAKE_FADE_IN_MS = 2200;
function snakeFadeAlpha(progress, duration, fadeOutMs = SNAKE_FADE_OUT_MS) {
  const fadeInMs = Math.min(SNAKE_FADE_IN_MS, duration * 0.5);
  const elapsedMs = progress * duration;
  const fadeIn = elapsedMs < fadeInMs ? Math.max(0, elapsedMs / fadeInMs) : 1;
  const cap = Math.min(fadeOutMs, duration * 0.5);
  const remainingMs = (1 - progress) * duration;
  const fadeOut = remainingMs < cap ? Math.max(0, remainingMs / cap) : 1;
  return Math.min(1, fadeIn, fadeOut);
}
if (typeof window !== 'undefined') window.snakeFadeAlpha = snakeFadeAlpha; // test seam

// Original signal weave informed by the interconnected symmetry and pathway
// quality of Shipibo-Konibo kene. It deliberately avoids reproducing a
// traditional motif or claiming cultural meaning.
function _initClankerKeneWeave() {
  const snakeSeed = Math.random() * 10000;
  const upper = [
    [0, .5], [.09, .5], [.09, .28], [.22, .28], [.22, .1], [.4, .1], [.4, .36], [.5, .36],
    [.5, .5], [.5, .64], [.6, .64], [.6, .9], [.78, .9], [.78, .72], [.91, .72], [.91, .5], [1, .5],
  ];
  const lower = upper.map(([x, y]) => [x, 1 - y]);
  const outer = [...upper, ...lower.slice(0, -1).reverse()];
  _mountClankerEffect({
    id: 'clanker-kene-weave-canvas',
    bodyClass: 'bg-pattern-clanker-kene-weave',
    build: ({ width, height, size, safeBounds, dpr, colors, outline, intensity }) => {
      const phase = value => {
        if (typeof window.__kenePhaseProbe !== 'function') return;
        try { window.__kenePhaseProbe(value); } catch (_) { /* diagnostic only */ }
      };
      phase('build-start');
      const columns = Math.max(4, Math.ceil(width / (220 * size)));
      const rows = Math.max(4, Math.ceil(height / (220 * size)));
      const tileWidth = safeBounds.width / columns;
      const tileHeight = safeBounds.height / rows;
      // Keep a full tile of connected geometry beyond each edge. The canvas
      // clips it to the chat pane, so the visible corners inherit neighboring
      // motifs instead of exposing the grid's finite boundary.
      // The canvas is already clipped to the chat pane; constructing a full
      // extra tile ring here multiplied first-paint raster work without adding
      // visible pixels. Keep the geometry bounded to the requested viewport.
      const gridMargin = 0;
      const firstColumn = -gridMargin;
      const firstRow = -gridMargin;
      const lastColumn = columns + gridMargin;
      const lastRow = rows + gridMargin;
      const paths = [];
      const junctions = [];
      const place = (points, column, row) => points.map(([x, y]) => ({
        x: safeBounds.left + (column + x) * tileWidth,
        y: safeBounds.top + (row + y) * tileHeight,
      }));
      const placeRotated = (points, centerX, centerY) => points.map(([x, y]) => ({
        x: centerX - (y - .5) * tileWidth,
        y: centerY + (x - .5) * tileHeight,
      }));
      const addMotif = (transform, color, layer, pair) => {
        paths.push({ points: transform(upper), color, layer, pair, mirror: 0 });
        paths.push({ points: transform(lower), color: (color + 2) % 6, layer, pair, mirror: 1 });
      };

      for (let row = firstRow; row < lastRow; row += 1) {
        for (let column = firstColumn; column < lastColumn; column += 1) {
          const color = ((row * 2 + column) % 6 + 6) % 6;
          addMotif(points => place(points, column, row), color, 0, `h:${row}:${column}`);
        }
      }
      phase(`paths:${paths.length}:junctions:${junctions.length}`);

      for (let row = firstRow; row < lastRow; row += 1) {
        for (let column = firstColumn; column <= columns + gridMargin; column += 1) {
          const junction = {
            x: safeBounds.left + column * tileWidth,
            y: safeBounds.top + (row + .5) * tileHeight,
            color: ((row * 2 + column) % 6 + 6) % 6,
          };
          junctions.push(junction);
          addMotif(
            points => placeRotated(points, junction.x, junction.y),
            (junction.color + 3) % 6,
            1,
            `v:${row}:${column}`,
          );
        }
      }

      // Offset duplicate: same grid shifted right half a column, down half a row
      for (let row = firstRow; row < lastRow; row += 1) {
        for (let column = firstColumn; column < lastColumn; column += 1) {
          const color = ((row * 2 + column + 3) % 6 + 6) % 6;
          addMotif(points => place(points, column + .5, row + .5), color, 0, `h2:${row}:${column}`);
        }
      }
      for (let row = firstRow; row < lastRow; row += 1) {
        for (let column = firstColumn; column <= columns + gridMargin; column += 1) {
          const junction = {
            x: safeBounds.left + (column + .5) * tileWidth,
            y: safeBounds.top + (row + 1) * tileHeight,
            color: ((row * 2 + column + 3) % 6 + 6) % 6,
          };
          junctions.push(junction);
          addMotif(
            points => placeRotated(points, junction.x, junction.y),
            (junction.color + 3) % 6,
            1,
            `v2:${row}:${column}`,
          );
        }
      }

      const graph = new Map();
      const pointKey = point => `${point.x.toFixed(3)}:${point.y.toFixed(3)}`;
      const graphPoint = point => {
        const key = pointKey(point);
        if (!graph.has(key)) graph.set(key, { x: point.x, y: point.y, key, neighbors: new Map() });
        return graph.get(key);
      };
      for (const path of paths) {
        for (let index = 1; index < path.points.length; index += 1) {
          const a = graphPoint(path.points[index - 1]);
          const b = graphPoint(path.points[index]);
          a.neighbors.set(b.key, b);
          b.neighbors.set(a.key, a);
        }
      }
      phase(`graph:${graph.size}`);

      const junctionNodes = junctions.map(junction => graph.get(pointKey(junction))).filter(Boolean);
      // Let animated signals begin and travel through the same overscan ring
      // as the static pattern, so their tails can enter and leave the pane.
      const overscanLeft = safeBounds.left - tileWidth;
      const overscanRight = safeBounds.right + tileWidth;
      const overscanTop = safeBounds.top - tileHeight;
      const overscanBottom = safeBounds.bottom + tileHeight;
      const insideOverscan = node => node
        && node.x >= overscanLeft && node.x <= overscanRight
        && node.y >= overscanTop && node.y <= overscanBottom;
      const overscanStarts = junctionNodes.filter(insideOverscan);
      const randomWalk = seed => {
        let current = overscanStarts[Math.floor(_clankerNoise(seed) * overscanStarts.length)];
        if (!current) return [];
        let previous = null;
        const points = [{ x: current.x, y: current.y }];
        // Keep route construction bounded: 64 snakes reuse these routes, so
        // generating a small deterministic pool avoids a long synchronous
        // first paint while retaining enough geometry for the stress case.
        for (let step = 0; step < 48; step += 1) {
          let candidates = [...current.neighbors.values()].filter(node => node !== previous && insideOverscan(node));
          if (!candidates.length) candidates = [...current.neighbors.values()].filter(insideOverscan);
          if (!candidates.length) break;
          const choice = Math.floor(_clankerNoise(seed + step * 37 + current.x * .013 + current.y * .017) * candidates.length);
          const next = candidates[Math.min(choice, candidates.length - 1)];
          if (!next) break;
          points.push({ x: next.x, y: next.y });
          previous = current;
          current = next;
        }
        return points;
      };
      const snakePoints = Array.from({ length: 36 }, (_, index) => randomWalk(snakeSeed + index * 113))
        .filter(route => route.length > 1)
        .slice(0, 24);
      const maxSnakeStep = snakePoints.reduce((maximum, route) => Math.max(maximum,
        ...route.slice(1).map((point, index) => Math.hypot(point.x - route[index].x, point.y - route[index].y))), 0);
      const snakeRoutes = snakePoints.map(points => {
        const cumulative = [0];
        for (let index = 1; index < points.length; index += 1) {
          cumulative.push(cumulative[index - 1] + Math.hypot(
            points[index].x - points[index - 1].x,
            points[index].y - points[index - 1].y,
          ));
        }
        return { points, cumulative, total: cumulative[cumulative.length - 1] || 1 };
      });
      phase(`routes:${snakeRoutes.length}`);
      // A snake owns its route and lifecycle for its entire lifetime.  The
      // previous renderer selected a new random route from the frame clock,
      // so changing speed or crossing the 16s boundary could teleport the
      // head.  Keep this small state beside the immutable route geometry and
      // only choose a successor when a lifetime reaches an endpoint.
      const snakes = Array.from({ length: 64 }, (_, index) => {
        const seed = snakeSeed + index * 131;
        return {
          routeIndex: snakeRoutes.length ? Math.floor(_clankerNoise(seed) * snakeRoutes.length) : 0,
          reverse: _clankerNoise(seed + 17) > .5,
          progress: _clankerNoise(seed + 23),
          cycle: 0,
          lengthNoise: _clankerNoise(seed + 43),
          lifetimeNoise: _clankerNoise(seed + 61),
          speedNoise: _clankerNoise(seed + 71),
          lastTime: 0,
          transitionAlpha: 1,
        };
      });
      // Static geometry is identical across frames. Rasterizing it once keeps
      // the animation loop focused on the small set of moving signals. The
      // raster is a named function so a palette/intensity change can repaint
      // it without rebuilding the scene or restarting snake trajectories (T08).
      const staticCanvas = document.createElement('canvas');
      const pixelRatio = Math.max(1, Math.min(2, dpr || 1));
      staticCanvas.width = Math.max(1, Math.floor(width * pixelRatio));
      staticCanvas.height = Math.max(1, Math.floor(height * pixelRatio));
      const rasterizeStatic = (staticColors, staticOutline, staticIntensity, staticSize) => {
        const staticCtx = staticCanvas.getContext('2d');
        if (!staticCtx) return;
        staticCtx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
        staticCtx.clearRect(0, 0, width, height);
        staticCtx.lineCap = 'round';
        staticCtx.lineJoin = 'round';
        paths.forEach(path => {
          staticCtx.beginPath();
          path.points.forEach((point, pointIndex) => pointIndex
            ? staticCtx.lineTo(point.x, point.y)
            : staticCtx.moveTo(point.x, point.y));
          staticCtx.strokeStyle = staticOutline;
          staticCtx.lineWidth = 3.4 * staticSize;
          staticCtx.globalAlpha = staticIntensity * (path.layer ? .2 : .24);
          staticCtx.stroke();
          staticCtx.strokeStyle = staticColors[path.color];
          staticCtx.lineWidth = 7 * staticSize;
          staticCtx.globalAlpha = staticIntensity * .025;
          staticCtx.stroke();
          staticCtx.strokeStyle = staticColors[path.color];
          staticCtx.lineWidth = 1.2 * staticSize;
          staticCtx.globalAlpha = staticIntensity * (path.layer ? .3 : .36);
          staticCtx.stroke();
        });
        junctions.forEach(junction => {
          staticCtx.fillStyle = staticColors[junction.color];
          staticCtx.globalAlpha = staticIntensity * .06;
          staticCtx.beginPath();
          staticCtx.arc(junction.x, junction.y, 7 * staticSize, 0, Math.PI * 2);
          staticCtx.fill();
          staticCtx.globalAlpha = staticIntensity * .7;
          staticCtx.beginPath();
          staticCtx.arc(junction.x, junction.y, 1.8 * staticSize, 0, Math.PI * 2);
          staticCtx.fill();
        });
        staticCtx.globalAlpha = 1;
      };
      rasterizeStatic(colors, outline, intensity, size);
      phase('static-done');
      // Bounded grid paint field — separate from snake bodies. Cells deposit
      // color where snakes visit and decay over time; the field is recreated
      // (cleared) only on a topology reset (scene rebuild). Off means no new
      // paint while existing paint keeps fading.
      const paintCell = Math.max(18, Math.min(tileWidth, tileHeight) / 3.2);
      const paintCols = Math.max(4, Math.min(96, Math.ceil(width / paintCell)));
      const paintRows = Math.max(4, Math.min(96, Math.ceil(height / paintCell)));
      const paintField = {
        cols: paintCols,
        rows: paintRows,
        cellW: width / paintCols,
        cellH: height / paintRows,
        colors: new Uint8Array(paintCols * paintRows),
        alphas: new Float32Array(paintCols * paintRows),
        deposit(x, y, colorIndex, amount) {
          const column = Math.floor(x / this.cellW);
          const row = Math.floor(y / this.cellH);
          if (column < 0 || row < 0 || column >= this.cols || row >= this.rows) return;
          const index = row * this.cols + column;
          this.colors[index] = ((colorIndex % 6) + 6) % 6;
          this.alphas[index] = Math.min(1, this.alphas[index] + amount);
        },
        decay(elapsedMs) {
          if (!elapsedMs) return;
          // Half-life ≈ 2.6s: paint fades out steadily whether or not new
          // deposits are arriving.
          const factor = Math.exp(-elapsedMs / 2600);
          const alphas = this.alphas;
          for (let index = 0; index < alphas.length; index += 1) {
            if (alphas[index] > 0) alphas[index] *= factor;
          }
        },
        clear() {
          this.alphas.fill(0);
        },
      };
      return { paths, junctions, snakePoints, maxSnakeStep: maxSnakeStep, snakeSeed,
        snakeRoutes, snakes, staticCanvas, rasterizeStatic, paintField,
        styleKey: `${intensity}:${outline}:${colors.join(',')}`,
        heads: [],
        layerVectors: [{ x: 1, y: 0 }, { x: 0, y: 1 }], mirroredPairs: paths.length / 2,
        junctionOffsetError: junctionNodes.length === junctions.length ? 0 : Infinity };
    },
    draw: (ctx, { width, height, time, reduced, scene, intensity, size, colors, outline }) => {
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      // Style-only updates repaint the static raster in place. Snake state is
      // untouched so trajectories continue through palette/intensity changes.
      const styleKey = `${intensity}:${outline}:${colors.join(',')}`;
      if (scene.styleKey !== styleKey) {
        if (typeof scene.rasterizeStatic === 'function') {
          scene.rasterizeStatic(colors, outline, intensity, size);
        }
        scene.styleKey = styleKey;
      }
      if (scene.staticCanvas) ctx.drawImage(scene.staticCanvas, 0, 0, scene.staticCanvas.width, scene.staticCanvas.height, 0, 0, width, height);
      const renderTime = reduced ? 0 : time;

      // ── Grid paint trail (S25) ──
      // Separate from snake bodies: a bounded cell field that receives color
      // where snakes visit and decays on its own clock. `snakePaintTrail` off
      // stops deposits; existing paint keeps fading to zero.
      const paintTrailOn = getBackgroundEffectControlValue('clanker-kene-weave', 'snakePaintTrail', false);
      const paintField = scene.paintField;
      const paintElapsed = (!reduced && scene.paintLastTime && renderTime > scene.paintLastTime)
        ? Math.min(80, renderTime - scene.paintLastTime) : 0;
      if (!reduced) scene.paintLastTime = renderTime;
      if (paintField) {
        if (paintElapsed) paintField.decay(paintElapsed);
        // Paint is drawn under the snakes as the residue they leave behind.
        // Deposits from this frame land here next frame (one-frame lag is
        // imperceptible and keeps paint out of the snake stroke path).
        const { cols, rows, cellW, cellH, colors: paintColors, alphas: paintAlphas } = paintField;
        for (let row = 0; row < rows; row += 1) {
          for (let column = 0; column < cols; column += 1) {
            const index = row * cols + column;
            const alpha = paintAlphas[index];
            if (alpha <= 0.012) continue;
            ctx.fillStyle = colors[paintColors[index] % colors.length];
            ctx.globalAlpha = intensity * alpha * .26;
            ctx.fillRect(column * cellW, row * cellH, cellW + .5, cellH + .5);
          }
        }
      }

      const snakeCount = getBackgroundEffectControlValue('clanker-kene-weave', 'snakeCount', 7);
      const snakeSpeedPct = getBackgroundEffectControlValue('clanker-kene-weave', 'snakeSpeed', 100) / 100;
      const speedVariationOn = getBackgroundEffectControlValue('clanker-kene-weave', 'snakeSpeedVariationToggle', false);
      const speedVariation = getBackgroundEffectControlValue('clanker-kene-weave', 'snakeSpeedVariation', 30) / 100;
      const lengthVariation = getBackgroundEffectControlValue('clanker-kene-weave', 'snakeLengthVariation', 0) / 100;
      const lifetimeVariation = getBackgroundEffectControlValue('clanker-kene-weave', 'snakeLifetimeVariation', 0) / 100;
      const shorterLastLonger = getBackgroundEffectControlValue('clanker-kene-weave', 'shorterLastLonger', false);
      const shorterLifetimeScale = getBackgroundEffectControlValue('clanker-kene-weave', 'shorterLifetimeScale', 100) / 100;
      const longerDisappearSooner = getBackgroundEffectControlValue('clanker-kene-weave', 'longerDisappearSooner', false);
      const longerLifetimeScale = getBackgroundEffectControlValue('clanker-kene-weave', 'longerLifetimeScale', 100) / 100;
      const baseTraverse = 16000 / snakeSpeedPct;
      for (let signalIndex = 0; signalIndex < snakeCount; signalIndex += 1) {
        if (!scene.snakeRoutes.length || !scene.snakes?.[signalIndex]) break;
        const snake = scene.snakes[signalIndex];
        const previousTime = snake.lastTime;
        const elapsed = previousTime && renderTime > previousTime
          ? Math.min(80, renderTime - previousTime) : 0;
        snake.lastTime = renderTime;
        const random = scene.snakeSeed + signalIndex * 131 + snake.cycle * 47;
        const tailSteps = Math.max(8, Math.round(64 * (1 + (snake.lengthNoise * 2 - 1) * lengthVariation * .6)));
        const lengthRatio = tailSteps / 64;
        let traverseTime = baseTraverse;
        if (speedVariationOn) {
          traverseTime *= 1 + (snake.speedNoise * 2 - 1) * speedVariation * .45;
        }
        let duration = traverseTime * (1 + (snake.lifetimeNoise * 2 - 1) * lifetimeVariation * .45);
        if (shorterLastLonger && lengthRatio < 1) duration *= 1 + (1 - lengthRatio) * shorterLifetimeScale;
        if (longerDisappearSooner && lengthRatio > 1) duration *= Math.max(.2, 1 - (lengthRatio - 1) * longerLifetimeScale);
        // Integrate the current velocity.  Changing speed/lifetime controls
        // therefore changes only the next increment; the current head remains
        // where it was instead of being recomputed from a new global phase.
        snake.progress += elapsed / Math.max(1, duration);
        if (snake.progress >= 1) {
          snake.progress %= 1;
          const oldRoute = scene.snakeRoutes[snake.routeIndex];
          const anchor = snake.reverse ? oldRoute.points[0] : oldRoute.points[oldRoute.points.length - 1];
          const nextReverse = _clankerNoise(random + 17) > .5;
          let bestIndex = snake.routeIndex;
          let bestDistance = Infinity;
          scene.snakeRoutes.forEach((candidate, candidateIndex) => {
            const endpoint = nextReverse ? candidate.points[candidate.points.length - 1] : candidate.points[0];
            const distance = Math.hypot(endpoint.x - anchor.x, endpoint.y - anchor.y);
            if (distance < bestDistance) {
              bestDistance = distance;
              bestIndex = candidateIndex;
            }
          });
          snake.routeIndex = bestIndex;
          snake.reverse = nextReverse;
          snake.cycle += 1;
          snake.lengthNoise = _clankerNoise(scene.snakeSeed + signalIndex * 131 + snake.cycle * 47 + 43);
          snake.lifetimeNoise = _clankerNoise(scene.snakeSeed + signalIndex * 131 + snake.cycle * 47 + 61);
          snake.speedNoise = _clankerNoise(scene.snakeSeed + signalIndex * 131 + snake.cycle * 47 + 71);
          snake.transitionAlpha = 1;
        }
        const progress = Math.max(0, Math.min(1, snake.progress));
        const route = scene.snakeRoutes[snake.routeIndex];
        const reverse = snake.reverse;
        const headProgress = reverse ? 1 - progress : progress;
        const fade = Math.max(snakeFadeAlpha(progress, duration), snake.transitionAlpha);
        snake.transitionAlpha = Math.max(0, snake.transitionAlpha - elapsed / SNAKE_FADE_IN_MS);
        const headPoint = _pointOnCachedPolyline(route, headProgress);
        scene.heads[signalIndex] = {
          x: headPoint.x, y: headPoint.y, routeIndex: snake.routeIndex,
          reverse: snake.reverse, progress: snake.progress, cycle: snake.cycle,
        };
        // Deposit paint at the head (and a short trail of tail samples) so the
        // grid field reads as continuous coverage rather than dotted points.
        if (paintTrailOn && paintField) {
          const depositColor = Math.floor(_clankerNoise(random + 31) * colors.length);
          paintField.deposit(headPoint.x, headPoint.y, depositColor, .34);
          for (let paintStep = 1; paintStep <= 3; paintStep += 1) {
            const paintProgress = headProgress + (reverse ? paintStep : -paintStep) * .0024;
            if (paintProgress < 0 || paintProgress > 1) continue;
            const paintPoint = _pointOnCachedPolyline(route, paintProgress);
            paintField.deposit(paintPoint.x, paintPoint.y, depositColor, .18);
          }
        }
        const tail = [];
        for (let step = tailSteps; step >= 0; step -= 1) {
          const pointProgress = headProgress + (reverse ? step : -step) * .0024;
          if (pointProgress >= 0 && pointProgress <= 1) tail.push(_pointOnCachedPolyline(route, pointProgress));
        }
        if (tail.length < 2) continue;
        const signalColor = colors[Math.floor(_clankerNoise(random + 31) * colors.length)];
        // A separate path/stroke for every tail segment makes the first paint
        // monopolize Canvas2D (especially with the signal shadow enabled).
        // Tail opacity is monotonic, so a small number of buckets preserves
        // the taper while keeping path setup and software shadow work bounded.
        const tailSegments = tail.length - 1;
        const fadeBuckets = 8;
        for (const [strokeStyle, lineWidth, alpha] of [
          [outline, 7.5 * size, .72],
          [signalColor, 3 * size, 1],
        ]) {
          ctx.strokeStyle = strokeStyle;
          ctx.lineWidth = lineWidth;
          ctx.shadowColor = signalColor;
          ctx.shadowBlur = strokeStyle === outline ? 0 : 18 * size;
          for (let bucket = 0; bucket < fadeBuckets; bucket += 1) {
            const first = Math.max(1, Math.floor(bucket * tailSegments / fadeBuckets) + 1);
            const last = Math.min(tailSegments, Math.floor((bucket + 1) * tailSegments / fadeBuckets));
            if (first > last) continue;
            ctx.beginPath();
            ctx.moveTo(tail[first - 1].x, tail[first - 1].y);
            for (let index = first; index <= last; index += 1) {
              ctx.lineTo(tail[index].x, tail[index].y);
            }
            const midpoint = ((first + last) / 2) / tailSegments;
            ctx.globalAlpha = intensity * alpha * fade * Math.pow(midpoint, 2.25);
            ctx.stroke();
          }
        }
        ctx.shadowBlur = 0;
        const head = tail[tail.length - 1];
        scene.heads[signalIndex].x = head.x;
        scene.heads[signalIndex].y = head.y;
        ctx.beginPath();
        ctx.arc(head.x, head.y, 3.8 * size, 0, Math.PI * 2);
        ctx.fillStyle = signalColor;
        ctx.globalAlpha = intensity * fade;
        ctx.shadowColor = signalColor;
        ctx.shadowBlur = 18 * size;
        ctx.fill();
        ctx.shadowBlur = 0;
      }
    },
  });
}

// S25 LCARS redesign — replaces Radar Ripples (`clanker-radar` migrates to
// `clanker-lcars` via PATTERN_ALIASES). Composition: asymmetric rounded elbow
// rails near the margins, segmented bars, small status tiles and coordinated
// pulses/stepped scans. Center stays open for readable applets. No concentric
// rings. Palette is theme-relative (gold/blue in Clanker) with low-alpha
// cyan/lilac secondary detail. Reduced motion is composed static art.
function _initClankerLcars() {
  _mountClankerEffect({
    id: 'clanker-lcars-canvas',
    bodyClass: 'bg-pattern-clanker-lcars',
    build: ({ size, safeBounds }) => {
      const { left, top, right, bottom, width, height } = safeBounds;
      // Rail thickness tracks the margin inset so laptop/large/mobile all keep
      // a readable open center. Elbow radius is generous — the rounded outer
      // corner is the LCARS signature.
      const rail = Math.max(9, Math.min(26, safeBounds.inset * .82)) * Math.max(.65, size);
      const radius = rail * .92;
      // Asymmetric arm lengths: the top-left and bottom-right elbows are the
      // dominant frame; the opposite corners are quieter, shorter elbows.
      const armLongH = width * .30;
      const armLongV = height * .34;
      const armShortH = width * .17;
      const armShortV = height * .19;
      // Elbow = two capsules meeting at the outer corner (rounded on both).
      const elbows = [
        { x: left, y: top, dirX: 1, dirY: 1, armH: armLongH, armV: armLongV, colorIndex: 0, accentIndex: 1, primary: true },
        { x: right, y: bottom, dirX: -1, dirY: -1, armH: armLongH * .92, armV: armLongV * .86, colorIndex: 1, accentIndex: 0, primary: true },
        { x: right, y: top, dirX: -1, dirY: 1, armH: armShortH, armV: armShortV, colorIndex: 1, accentIndex: 0, primary: false },
        { x: left, y: bottom, dirX: 1, dirY: -1, armH: armShortH * .84, armV: armShortV * 1.08, colorIndex: 0, accentIndex: 1, primary: false },
      ].map(elbow => ({
        ...elbow,
        rail,
        radius,
        // Normalized capsule origins so draw() does not recompute sign cases.
        hx: elbow.dirX > 0 ? elbow.x : elbow.x - elbow.armH,
        hy: elbow.dirY > 0 ? elbow.y : elbow.y - rail,
        vx: elbow.dirX > 0 ? elbow.x : elbow.x - rail,
        vy: elbow.dirY > 0 ? elbow.y : elbow.y - elbow.armV,
      }));

      // Segmented bars sit just inside the long elbows. Gaps between segments
      // are the LCARS "discrete channel" look; pulses travel across them.
      const barGap = 4 * size;
      const barH = rail * .62;
      const makeBar = (y, x0, x1, seed, colorIndex) => {
        const segments = [];
        let cursor = x0;
        let index = 0;
        while (cursor < x1 - barH) {
          const noise = _clankerNoise(seed + index * 17);
          const segW = Math.min(x1 - cursor, barH * (1.6 + noise * 3.4));
          segments.push({
            x: cursor,
            y,
            w: segW,
            h: barH,
            colorIndex: (colorIndex + index) % 6,
            // Per-segment alpha band keeps the bar low-contrast overall.
            alphaBand: .34 + (index % 3) * .1,
            // Pulse phase seed (0..1) — coordinated pulses share the bar clock.
            phase: _clankerNoise(seed + index * 31 + 7),
          });
          cursor += segW + barGap;
          index += 1;
        }
        return { y, x0, x1, segments, seed, barH };
      };
      const bars = [
        makeBar(top + rail * 1.35, left + armLongH * .55, right - armShortH * 1.1, 11, 0),
        makeBar(bottom - rail * 1.35 - barH, left + armShortH * .7, right - armLongH * .48, 29, 1),
      ];

      // Small status tiles: rounded chips with a tiny level indicator. Placed
      // near the elbows, never in the center third of the composition.
      const tile = Math.max(10, rail * 1.15);
      const tiles = [
        { x: left + armLongH * .30, y: top + rail * 2.4, w: tile * 2.2, h: tile, colorIndex: 1, level: .62, phase: .12 },
        { x: left + armLongH * .30 + tile * 2.6, y: top + rail * 2.4, w: tile * 1.5, h: tile, colorIndex: 0, level: .38, phase: .58 },
        { x: right - armShortH * .55, y: bottom - rail * 2.4 - tile, w: tile * 1.9, h: tile, colorIndex: 1, level: .71, phase: .33 },
        { x: right - armShortH * .55, y: bottom - rail * 2.4 - tile * 2.3, w: tile * 1.3, h: tile, colorIndex: 0, level: .25, phase: .81 },
      ];

      // Quiet stepped scans: thin highlight strips that move in discrete steps
      // along a bar rather than sweeping continuously.
      const scans = bars.map((bar, index) => ({
        barIndex: index,
        stepCount: Math.max(6, bar.segments.length * 2),
        speed: 0.55 + index * 0.22,
        thickness: bar.barH * .38,
      }));

      return {
        elbows,
        bars,
        tiles,
        scans,
        rail,
        radius,
        barH,
        tile,
        safeBounds: { left, top, right, bottom, width, height },
      };
    },
    draw: (ctx, { time, reduced, scene, intensity, size, colors, outline }) => {
      // Reduced motion is a composed static art piece: geometry and resting
      // highlights only, pinned so a delayed repaint cannot move the scene.
      const renderTime = reduced ? 0 : time;
      const primary = colors[0];
      const accent = colors[1];
      const lilac = colors[5] || colors[1];
      // Low-alpha secondary cyan/lilac details. Cyan is the primary hue read
      // back at low alpha; lilac is the palette's lilac slot.
      const cyanDetail = primary;
      const drawCapsule = (x, y, w, h, r, color, alpha) => {
        ctx.beginPath();
        if (typeof ctx.roundRect === 'function') {
          ctx.roundRect(x, y, w, h, r);
        } else {
          // Ordinary fallback for engines without roundRect.
          ctx.rect(x, y, w, h);
        }
        ctx.fillStyle = color;
        ctx.globalAlpha = alpha;
        ctx.fill();
      };

      // ── Elbow rails ──
      for (const elbow of scene.elbows) {
        const baseAlpha = intensity * (elbow.primary ? .30 : .20);
        const edgeAlpha = intensity * (elbow.primary ? .46 : .30);
        // Horizontal capsule arm.
        drawCapsule(elbow.hx, elbow.hy, elbow.armH, elbow.rail, elbow.rail / 2,
          colors[elbow.colorIndex], baseAlpha);
        // Vertical capsule arm.
        drawCapsule(elbow.vx, elbow.vy, elbow.rail, elbow.armV, elbow.rail / 2,
          colors[elbow.colorIndex], baseAlpha);
        // Inner accent stripe along the primary elbows (low-alpha gold/blue).
        if (elbow.primary) {
          const stripe = elbow.rail * .28;
          drawCapsule(
            elbow.dirX > 0 ? elbow.hx + elbow.rail * .5 : elbow.hx + elbow.rail * .5,
            elbow.dirY > 0 ? elbow.hy + elbow.rail - stripe : elbow.hy,
            Math.max(6, elbow.armH - elbow.rail * .5), stripe, stripe / 2,
            colors[elbow.accentIndex], edgeAlpha * .55,
          );
          drawCapsule(
            elbow.dirX > 0 ? elbow.vx + elbow.rail - stripe : elbow.vx,
            elbow.dirY > 0 ? elbow.vy + elbow.rail * .5 : elbow.vy + elbow.rail * .5,
            stripe, Math.max(6, elbow.armV - elbow.rail * .5), stripe / 2,
            colors[elbow.accentIndex], edgeAlpha * .55,
          );
        }
        // Low-alpha cyan/lilac detail caps at the arm ends (quiet secondary).
        const endR = elbow.rail * .5;
        ctx.beginPath();
        ctx.arc(
          elbow.dirX > 0 ? elbow.hx + elbow.armH - endR : elbow.hx + endR,
          elbow.hy + endR, endR * .62, 0, Math.PI * 2,
        );
        ctx.fillStyle = cyanDetail;
        ctx.globalAlpha = intensity * .16;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(
          elbow.vx + endR,
          elbow.dirY > 0 ? elbow.vy + elbow.armV - endR : elbow.vy + endR,
          endR * .62, 0, Math.PI * 2,
        );
        ctx.fillStyle = lilac;
        ctx.globalAlpha = intensity * .14;
        ctx.fill();
      }

      // ── Segmented bars + coordinated pulses ──
      // One shared pulse clock per bar so connected segments light in sequence
      // (coordinated pulses through connected segments, no expanding rings).
      const pulsePeriod = 5200;
      for (const bar of scene.bars) {
        for (const segment of bar.segments) {
          drawCapsule(segment.x, segment.y, segment.w, segment.h, segment.h / 2,
            colors[segment.colorIndex], intensity * segment.alphaBand * .62);
        }
        // Coordinated pulse: a phase-locked highlight stepping across segments.
        const clock = ((renderTime / pulsePeriod) + bar.seed * .017) % 1;
        const pulseIndex = Math.floor(clock * bar.segments.length);
        const pulseSegment = bar.segments[pulseIndex];
        if (pulseSegment) {
          drawCapsule(pulseSegment.x, pulseSegment.y, pulseSegment.w, pulseSegment.h,
            pulseSegment.h / 2, accent, intensity * .34);
          // Quiet low-alpha cyan wash on the pulse segment.
          drawCapsule(pulseSegment.x, pulseSegment.y, pulseSegment.w, pulseSegment.h,
            pulseSegment.h / 2, cyanDetail, intensity * .10);
        }
      }

      // ── Status tiles ──
      for (const tile of scene.tiles) {
        drawCapsule(tile.x, tile.y, tile.w, tile.h, tile.h * .32,
          colors[tile.colorIndex], intensity * .22);
        // Level indicator: a short bar inside the tile whose length is the
        // tile's designed level, with a gentle breathing highlight.
        const breathe = reduced ? 1 : 1 + Math.sin(renderTime / 2400 + tile.phase * Math.PI * 2) * .12;
        const levelW = (tile.w - tile.h * .55) * Math.max(.12, Math.min(1, tile.level * breathe));
        drawCapsule(
          tile.x + tile.h * .28,
          tile.y + tile.h * .34,
          levelW,
          tile.h * .32,
          tile.h * .16,
          colors[(tile.colorIndex + 1) % colors.length],
          intensity * .52,
        );
        // Low-alpha lilac tick at the tile's far edge.
        drawCapsule(
          tile.x + tile.w - tile.h * .38,
          tile.y + tile.h * .22,
          tile.h * .16,
          tile.h * .56,
          tile.h * .08,
          lilac,
          intensity * .18,
        );
      }

      // ── Quiet stepped scans ──
      // Discrete steps along the bar — not a continuous sweep. Each step is a
      // short highlight strip in a segment slot.
      for (const scan of scene.scans) {
        const bar = scene.bars[scan.barIndex];
        if (!bar || !bar.segments.length) continue;
        const step = reduced
          ? Math.floor(scan.stepCount * .38)
          : Math.floor((renderTime / 2800 * scan.speed) % scan.stepCount);
        const progress = step / scan.stepCount;
        const x = bar.x0 + (bar.x1 - bar.x0) * progress;
        const w = Math.min(bar.barH * 1.4, bar.x1 - x);
        if (w <= 0) continue;
        drawCapsule(x, bar.y + (bar.barH - scan.thickness) / 2, w, scan.thickness,
          scan.thickness / 2, cyanDetail, intensity * .20);
      }

      // Resting outline ticks at the elbow corners — quiet secondary structure.
      ctx.globalAlpha = intensity * .12;
      ctx.strokeStyle = outline;
      ctx.lineWidth = 1 * size;
      for (const elbow of scene.elbows) {
        const cx = elbow.dirX > 0 ? elbow.x + elbow.rail * .5 : elbow.x - elbow.rail * .5;
        const cy = elbow.dirY > 0 ? elbow.y + elbow.rail * .5 : elbow.y - elbow.rail * .5;
        ctx.beginPath();
        ctx.moveTo(cx - elbow.rail * .28, cy);
        ctx.lineTo(cx + elbow.rail * .28, cy);
        ctx.moveTo(cx, cy - elbow.rail * .28);
        ctx.lineTo(cx, cy + elbow.rail * .28);
        ctx.stroke();
      }
    },
  });
}

function _clankerNoise(seed) {
  const value = Math.sin(seed * 12.9898 + 78.233) * 43758.5453;
  return value - Math.floor(value);
}

// T13: curated supported graphemes — not broad code-point ranges. The rain
// string below is already a curated emoji inventory (including multi-code-point
// sequences such as ZWJ families and flag-like forms). Drift samples it through
// the grapheme segmenter so multi-code-point emoji stay intact, and falls back
// to a single usable glyph if the pool is ever empty.
const _CLANKER_DRIFT_FALLBACK_GLYPH = '◆';
const _CLANKER_MATRIX_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@#$%^&*()_+-=[]{}|;:,.<>?/~`';
const _CLANKER_RAIN_EMOJI_CHARS = [
  '😀😃😄😁😆😅😂🤣🥲😊😇🙂🙃😉😌😍🥰😘😗😙😚😋😛😝😜🤪🤨🧐🤓😎🥸🤩🥳😏😒😞😔😟😕🙁😣😖😫😩🥺😢😭😤😠😡🤬🤯😳🥵🥶😱😨😰😥😓🤗🤔🫣🤭🫢🫡🤫🫠🤥😶🫥😐🫤😑🙄😯😦😧😮😲🥱😴🤤😪😮‍💨😵😵‍💫🤐🥴🤢🤮🤧😷🤒🤕🤑🤠😈👿👹👺🤡💩👻💀☠👽👾🤖🎃🙈🙉🙊😺😸😹😻😼😽🙀😿😾💋💌💘💝💖💗💓💞💕💟❣💔❤️‍🔥❤️‍🩹❤🩷🧡💛💚💙🩵💜🤎🖤🩶🤍💯💢💥💫💦💨🕳💬👁‍🗨🗨🗯💭💤',
  '🐵🐒🦍🦧🐶🐕🦮🐕‍🦺🐩🐺🦊🦝🐱🐈🐈‍⬛🦁🐯🐅🐆🐴🫎🫏🐎🦄🦓🦌🦬🐮🐂🐃🐄🐷🐖🐗🐽🐏🐑🐐🐪🐫🦙🦒🐘🦣🦏🦛🐭🐁🐀🐹🐰🐇🐿🦫🦔🦇🐻🐻‍❄🐨🐼🦥🦦🦨🦘🦡🐾🦃🐔🐓🐣🐤🐥🐦🐧🕊🦅🦆🦢🦉🦤🪶🦩🦚🦜🪽🐦‍⬛🪿🐸🐊🐢🦎🐍🐲🐉🦕🦖🐳🐋🐬🦭🐟🐠🐡🦈🐙🐚🪸🪼🐌🦋🐛🐜🐝🪲🐞🦗🪳🕷🕸🦂🦟🪰🪱🦠💐🌸💮🪷🏵🌹🥀🌺🌻🌼🌷🪻🌱🪴🌲🌳🌴🌵🌾🌿☘🍀🍁🍂🍃🪹🪺🍄🌰🦀🦞🦐🦑',
  '🍇🍈🍉🍊🍋🍌🍍🥭🍎🍏🍐🍑🍒🍓🫐🥝🍅🫒🥥🥑🍆🥔🥕🌽🌶🫑🥒🥬🥦🧄🧅🥜🫘🌰🫚🫛🍞🥐🥖🫓🥨🥯🥞🧇🧀🍖🍗🥩🥓🍔🍟🍕🌭🥪🌮🌯🫔🥙🧆🥚🍳🥘🍲🫕🥣🥗🍿🧈🧂🥫🍱🍘🍙🍚🍛🍜🍝🍠🍢🍣🍤🍥🥮🍡🥟🥠🥡🦪🍦🍧🍨🍩🍪🎂🍰🧁🥧🍫🍬🍭🍮🍯🍼🥛☕🫖🍵🍶🍾🍷🍸🍹🍺🍻🥂🥃🫗🥤🧋🧃🧉🧊🥢🍽🍴🥄🔪🫙🏺',
].join('');
const _CLANKER_EMOJI_FONT = '"Noto Color Emoji", "Noto Emoji", "Apple Color Emoji", "Segoe UI Emoji", sans-serif';

let _clankerDriftGlyphPoolCache = null;
function _clankerDriftGlyphPool() {
  if (_clankerDriftGlyphPoolCache) return _clankerDriftGlyphPoolCache;
  const pool = _clankerGraphemes(_CLANKER_RAIN_EMOJI_CHARS).filter(glyph => glyph && glyph.trim());
  _clankerDriftGlyphPoolCache = pool.length ? pool : [_CLANKER_DRIFT_FALLBACK_GLYPH];
  return _clankerDriftGlyphPoolCache;
}

function _clankerEmoji(seed) {
  const pool = _clankerDriftGlyphPool();
  const index = Math.floor(_clankerNoise(seed) * pool.length) % pool.length;
  return pool[index] || _CLANKER_DRIFT_FALLBACK_GLYPH;
}

function _buildClankerDriftScene({ width, height, size, safeBounds, dpr }) {
  const baseCount = Math.max(140, Math.ceil(width * height / 5600));
  const count = Math.min(720, Math.ceil(baseCount * 2.5));
  return {
    baseCount,
    spriteDpr: dpr || 1,
    emojiSprites: new Map(),
    shards: Array.from({ length: count }, (_, index) => {
      const seed = index * 47 + 11;
      return {
        x: safeBounds.left + _clankerNoise(seed) * safeBounds.width,
        y: safeBounds.top + _clankerNoise(seed + 5) * safeBounds.height,
        baseRadius: 9,
        radiusNoise: _clankerNoise(seed + 9) * 2 - 1,
        stretch: .7 + _clankerNoise(seed + 13) * .55,
        rotation: _clankerNoise(seed + 17) * Math.PI * 2,
        angle: 0,
        color: index % 6,
        shape: index % 4,
        phase: _clankerNoise(seed + 23) * Math.PI * 2,
        drift: (3 + _clankerNoise(seed + 29) * 9) * size,
        driftSpeedNoise: _clankerNoise(seed + 43),
        rotationNoise: _clankerNoise(seed + 47),
        rotationSpeedNoise: _clankerNoise(seed + 53),
        rotationDirection: _clankerNoise(seed + 59) < .5 ? -1 : 1,
        glowNoise: _clankerNoise(seed + 31),
        intensityNoise: _clankerNoise(seed + 37) * 2 - 1,
        emoji: _clankerEmoji(seed + 41),
      };
    }),
  };
}

// T11: raster cache is quantized (so live size tweaks cannot mint one entry
// per continuous font size), bounded by an LRU cap, and released on unmount.
const _EMOJI_SPRITE_MAX_ENTRIES = 48;
const _EMOJI_SPRITE_SIZE_STEP = 4;

function _quantizeEmojiSpriteSize(pixelFontSize) {
  return Math.max(12, Math.round(pixelFontSize / _EMOJI_SPRITE_SIZE_STEP) * _EMOJI_SPRITE_SIZE_STEP);
}

function _clankerEmojiSprite(scene, emoji, fontSize, color) {
  const dpr = scene.spriteDpr || 1;
  const pixelFontSize = _quantizeEmojiSpriteSize(Math.max(12, Math.round(fontSize * dpr)));
  const key = `${emoji}\u0000${pixelFontSize}\u0000${color}`;
  const cached = scene.emojiSprites.get(key);
  if (cached) {
    // LRU touch: re-insert so Map iteration order tracks recency.
    scene.emojiSprites.delete(key);
    scene.emojiSprites.set(key, cached);
    return cached;
  }

  const padding = Math.ceil(pixelFontSize * .28);
  const sprite = document.createElement('canvas');
  const spriteCtx = sprite.getContext('2d');
  if (!spriteCtx) return null;
  spriteCtx.font = `${pixelFontSize}px ${_CLANKER_EMOJI_FONT}`;
  const metrics = spriteCtx.measureText(emoji);
  sprite.width = Math.max(pixelFontSize + padding * 2, Math.ceil(metrics.width + padding * 2));
  sprite.height = Math.ceil(pixelFontSize * 1.45 + padding * 2);
  spriteCtx.font = `${pixelFontSize}px ${_CLANKER_EMOJI_FONT}`;
  spriteCtx.textAlign = 'center';
  spriteCtx.textBaseline = 'middle';
  spriteCtx.fillStyle = color;
  spriteCtx.fillText(emoji, sprite.width / 2, sprite.height / 2);
  const result = {
    canvas: sprite,
    width: sprite.width / dpr,
    height: sprite.height / dpr,
  };
  // Bounded LRU: evict the oldest entry before inserting past the cap.
  if (scene.emojiSprites.size >= _EMOJI_SPRITE_MAX_ENTRIES) {
    const oldest = scene.emojiSprites.keys().next().value;
    if (oldest !== undefined) scene.emojiSprites.delete(oldest);
  }
  scene.emojiSprites.set(key, result);
  return result;
}

function _clankerDriftControls(pattern) {
  return {
    driftSpeed: getBackgroundEffectControlValue(pattern, 'driftSpeed', 100) / 100,
    driftSpeedVariation: getBackgroundEffectControlValue(pattern, 'driftSpeedVariation', 20) / 100,
    sizeVariation: getBackgroundEffectControlValue(pattern, 'gemSizeVariation', 100) / 100,
    intensityVariation: getBackgroundEffectControlValue(pattern, 'intensityVariation', 100) / 100,
    middleIntensity: getBackgroundEffectControlValue(pattern, 'middleIntensity', 100) / 100,
    totalQuantity: getBackgroundEffectControlValue(pattern, 'totalQuantity', 100) / 100,
    glowLikelihood: getBackgroundEffectControlValue(pattern, 'glowLikelihood', 11) / 100,
    rotationLikelihood: getBackgroundEffectControlValue(pattern, 'rotationLikelihood', 35) / 100,
    rotationSpeed: getBackgroundEffectControlValue(pattern, 'rotationSpeed', 100) / 100,
    rotationSpeedVariation: getBackgroundEffectControlValue(pattern, 'rotationSpeedVariation', 30) / 100,
  };
}

function _clankerDriftFrameLerp(scene, time) {
  const previousTime = scene.driftRenderTime;
  scene.driftRenderTime = time;
  if (!Number.isFinite(previousTime)) return { lerp: 1, elapsed: 0 };
  if (time <= previousTime) return { lerp: 0, elapsed: 0 };
  const elapsed = Math.min(50, Math.max(0, time - previousTime));
  return { lerp: 1 - Math.exp(-elapsed / 48), elapsed };
}

function _clankerDriftState(shard, index, time, size, controls, lerpAlpha, elapsedMs = 0) {
  const driftSpeed = controls.driftSpeed * (1 + (shard.driftSpeedNoise * 2 - 1) * controls.driftSpeedVariation * .65);
  const driftX = Math.sin(time * driftSpeed / (5900 + index % 7 * 340) + shard.phase) * shard.drift * driftSpeed;
  const driftY = Math.cos(time * driftSpeed / (7000 + index % 5 * 410) + shard.phase) * shard.drift * .72 * driftSpeed;
  const rawX = shard.x + driftX;
  const rawY = shard.y + driftY;
  if (!Number.isFinite(shard.renderX) || !Number.isFinite(shard.renderY)) {
    shard.renderX = rawX;
    shard.renderY = rawY;
  } else {
    shard.renderX += (rawX - shard.renderX) * lerpAlpha;
    shard.renderY += (rawY - shard.renderY) * lerpAlpha;
  }
  const rotationSpeed = controls.rotationSpeed
    * (1 + (shard.rotationSpeedNoise * 2 - 1) * controls.rotationSpeedVariation * .65);
  // T12: integrate angle over delta time so a speed change does not jump phase.
  if (shard.rotationNoise < controls.rotationLikelihood && elapsedMs > 0) {
    shard.angle = (shard.angle || 0) + elapsedMs * .00045 * rotationSpeed * shard.rotationDirection;
  }
  return {
    x: shard.renderX,
    y: shard.renderY,
    radius: Math.max(1.5 * size, (shard.baseRadius + shard.radiusNoise * 5 * controls.sizeVariation) * size),
    bright: shard.glowNoise < controls.glowLikelihood,
    alphaScale: controls.middleIntensity * Math.max(0, 1 + shard.intensityNoise * .28 * controls.intensityVariation),
    rotation: shard.rotation + (shard.angle || 0),
  };
}

function _initClankerGemDrift() {
  _mountClankerEffect({
    id: 'clanker-gem-drift-canvas',
    bodyClass: 'bg-pattern-clanker-gem-drift',
    build: _buildClankerDriftScene,
    draw: (ctx, { width, height, time, reduced, scene, intensity, size, colors, outline }) => {
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      const controls = _clankerDriftControls('clanker-gem-drift');
      const renderTime = reduced ? 0 : time;
      const driftFrame = _clankerDriftFrameLerp(scene, renderTime);
      const canvasBounds = { left: 0, top: 0, right: width, bottom: height };
      const count = Math.min(scene.shards.length, Math.round(scene.baseCount * controls.totalQuantity));
      for (let index = 0; index < count; index += 1) {
        const shard = scene.shards[index];
        const { x, y, radius, bright, alphaScale, rotation } = _clankerDriftState(shard, index, renderTime, size, controls, driftFrame.lerp, driftFrame.elapsed);
        const extent = radius * Math.max(1, shard.stretch) + (bright ? 12 : 3) * size;
        const edgeAlpha = _clankerEdgeAlpha(x, y, extent, canvasBounds);
        if (!edgeAlpha) continue;
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(rotation);
        ctx.scale(shard.stretch, 1);
        ctx.beginPath();
        if (shard.shape === 0) {
          ctx.moveTo(0, -radius); ctx.lineTo(radius * .8, 0); ctx.lineTo(0, radius); ctx.lineTo(-radius * .8, 0);
        } else if (shard.shape === 1) {
          ctx.moveTo(-radius * .78, -radius * .35); ctx.lineTo(-radius * .18, -radius); ctx.lineTo(radius * .82, -radius * .42); ctx.lineTo(radius * .58, radius * .72); ctx.lineTo(-radius * .55, radius * .9);
        } else if (shard.shape === 2) {
          ctx.moveTo(0, -radius); ctx.lineTo(radius * .9, radius * .7); ctx.lineTo(-radius * .9, radius * .7);
        } else {
          ctx.moveTo(-radius * .72, -radius * .58); ctx.lineTo(radius * .42, -radius * .88); ctx.lineTo(radius, 0); ctx.lineTo(radius * .35, radius * .86); ctx.lineTo(-radius * .82, radius * .52);
        }
        ctx.closePath();
        ctx.fillStyle = colors[shard.color];
        ctx.strokeStyle = outline;
        ctx.lineWidth = (bright ? 2 : 1.4) * size;
        ctx.globalAlpha = Math.min(1, intensity * alphaScale * (bright ? .62 : .22)) * edgeAlpha;
        if (bright) { ctx.shadowColor = colors[shard.color]; ctx.shadowBlur = 9 * size; }
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.globalAlpha = Math.min(1, intensity * alphaScale * (bright ? .8 : .34)) * edgeAlpha;
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(0, -radius * .7); ctx.lineTo(0, 0); ctx.lineTo(radius * .5, radius * .34);
        ctx.strokeStyle = colors[(shard.color + 2) % colors.length];
        ctx.lineWidth = .9 * size;
        ctx.globalAlpha = Math.min(1, intensity * alphaScale * (bright ? .58 : .26)) * edgeAlpha;
        ctx.stroke();
        ctx.restore();
      }
    },
  });
}

function _initClankerEmojiDrift() {
  _mountClankerEffect({
    id: 'clanker-emoji-drift-canvas',
    bodyClass: 'bg-pattern-clanker-emoji-drift',
    build: _buildClankerDriftScene,
    draw: (ctx, { width, height, time, reduced, scene, intensity, size, colors }) => {
      const controls = _clankerDriftControls('clanker-emoji-drift');
      const renderTime = reduced ? 0 : time;
      const driftFrame = _clankerDriftFrameLerp(scene, renderTime);
      const canvasBounds = { left: 0, top: 0, right: width, bottom: height };
      ctx.imageSmoothingEnabled = true;
      const count = Math.min(scene.shards.length, Math.round(scene.baseCount * controls.totalQuantity));
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      for (let index = 0; index < count; index += 1) {
        const shard = scene.shards[index];
        const { x, y, radius, bright, alphaScale, rotation } = _clankerDriftState(shard, index, renderTime, size, controls, driftFrame.lerp, driftFrame.elapsed);
        const extent = radius * 1.5 + (bright ? 12 : 3) * size;
        const edgeAlpha = _clankerEdgeAlpha(x, y, extent, canvasBounds);
        if (!edgeAlpha) continue;
        const fontSize = Math.max(12, radius * 2.3);
        const sprite = _clankerEmojiSprite(scene, shard.emoji, fontSize, colors[shard.color]);
        if (!sprite) continue;
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(rotation);
        ctx.globalAlpha = Math.min(1, intensity * alphaScale * (bright ? .78 : .34)) * edgeAlpha;
        if (bright) { ctx.shadowColor = colors[shard.color]; ctx.shadowBlur = 12 * size; }
        ctx.drawImage(sprite.canvas, -sprite.width / 2, -sprite.height / 2, sprite.width, sprite.height);
        ctx.restore();
      }
    },
  });
}

function _clankerGraphemes(value) {
  if (typeof Intl !== 'undefined' && Intl.Segmenter) {
    const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
    return Array.from(segmenter.segment(value), part => part.segment);
  }
  return Array.from(value);
}

const _CLANKER_CODE_RAIN_EMOJI_GLYPHS = _clankerGraphemes(_CLANKER_RAIN_EMOJI_CHARS);
const _CLANKER_CODE_RAIN_COLLISION_SELECTORS = [
  '.chat-top-bar',
  '.chat-input-bar',
  '.msg-user',
  '.msg-ai',
  '.agent-thread-content',
  '.agent-tool-output',
  '.mimo-plan-dock',
  '.chat-context-popup',
  '.ctx-popup',
  '.toast.show',
  '.tour-hint',
  '[class*="popup"]:not(.hidden)',
  '[class*="popover"]:not(.hidden)',
  '.modal:not(.hidden) .modal-content',
  '.copal-tool-modal:not(.hidden) > *',
  '[role="dialog"]:not([aria-hidden="true"])',
  '.welcome-name',
  '.welcome-sub',
  '.welcome-tip',
];

function _clankerCodeRainPattern(mode) {
  return mode === 'emoji' ? 'clanker-emoji-rain' : 'clanker-matrix-rain';
}

function _clankerCodeRainControls(mode) {
  const pattern = _clankerCodeRainPattern(mode);
  return {
    quantity: getBackgroundEffectControlValue(pattern, 'splashQuantity', 1.4),
    speed: getBackgroundEffectControlValue(pattern, 'splashRainSpeed', 1),
    flicker: getBackgroundEffectControlValue(pattern, 'splashRainFlicker', .16),
    spread: getBackgroundEffectControlValue(pattern, 'splashRainSpread', 1),
    rainDown: getBackgroundEffectControlValue(pattern, 'splashRainDown', true),
    reverseChance: getBackgroundEffectControlValue(pattern, 'splashRainReverseChance', 0),
    rareUpward: getBackgroundEffectControlValue(pattern, 'splashRareUpward', true),
    waves: getBackgroundEffectControlValue(pattern, 'splashRainWaves', false),
    charVariety: getBackgroundEffectControlValue(pattern, 'splashCharVariety', 1),
    minOpacity: getBackgroundEffectControlValue(pattern, 'splashMinOpacity', .2),
    maxOpacity: getBackgroundEffectControlValue(pattern, 'splashMaxOpacity', 1),
    sizeVariance: getBackgroundEffectControlValue(pattern, 'splashSizeVariance', .45),
    colorVariance: getBackgroundEffectControlValue(pattern, 'splashColorVarianceEnabled', false)
      ? getBackgroundEffectControlValue(pattern, 'splashColorVariance', .32) : 0,
    bounce: getBackgroundEffectControlValue(pattern, 'splashBounce', 1.3),
    gravity: getBackgroundEffectControlValue(pattern, 'splashGravity', .2),
    collisionForce: getBackgroundEffectControlValue(pattern, 'splashCollisionForce', 2.2),
    emojiMix: mode === 'matrix' && getBackgroundEffectControlValue(pattern, 'splashEmojiMix', false),
    emojiRarity: getBackgroundEffectControlValue(pattern, 'splashEmojiRarity', 10000000),
  };
}

// Topology keys rebuild the droplet set. UI-only keys (advanced settings,
// direction, rare toggle, speed, bounce, …) are read live and must not reset
// droplets or the running simulation.
const _RAIN_TOPOLOGY_FIELDS = [
  'quantity', 'charVariety', 'sizeVariance', 'minOpacity', 'maxOpacity', 'emojiMix', 'emojiRarity',
];

function _clankerCodeRainTopologyKey(mode, width, height, size) {
  const controls = _clankerCodeRainControls(mode);
  const parts = _RAIN_TOPOLOGY_FIELDS.map(field => `${field}=${controls[field]}`);
  return `${mode}:${width}:${height}:${size}:${parts.join(':')}`;
}

function _clankerCodeRainDirection(seed, controls, rareSeed = seed + 59) {
  return composeRainDirection({
    rainDown: controls.rainDown,
    reverseChance: controls.reverseChance,
    rareUpward: controls.rareUpward,
    noise: _clankerNoise(seed),
    rareNoise: _clankerNoise(rareSeed),
  }).direction;
}

function _buildClankerCodeRainScene({ width, height, size, mode }) {
  const emoji = mode === 'emoji';
  const controls = _clankerCodeRainControls(mode);
  const minOpacity = Math.min(controls.minOpacity, controls.maxOpacity);
  const maxOpacity = Math.max(controls.minOpacity, controls.maxOpacity);
  const cell = Math.max(emoji ? 42 : 17, (emoji ? 52 : 21) * size);
  const streamCount = Math.max(emoji ? 22 : 46, Math.min(emoji ? 96 : 180, Math.ceil(width / cell * controls.quantity)));
  const glyphSize = Math.max(emoji ? 20 : 11, (emoji ? 26 : 14) * size);
  const glyphs = emoji ? _CLANKER_CODE_RAIN_EMOJI_GLYPHS : Array.from(_CLANKER_MATRIX_CHARS);
  const varietyLength = Math.max(1, Math.ceil(glyphs.length * controls.charVariety));
  const glyphPool = glyphs.slice(0, varietyLength);
  const emojiPool = _CLANKER_CODE_RAIN_EMOJI_GLYPHS;
  const pickChar = (seed, offset) => controls.emojiMix && _clankerNoise(seed + offset) < 1 / Math.max(1, controls.emojiRarity)
    ? emojiPool[Math.floor(_clankerNoise(seed + offset + 1) * emojiPool.length)]
    : glyphPool[Math.floor(_clankerNoise(seed + offset + 1) * glyphPool.length)];
  const streams = Array.from({ length: streamCount }, (_, index) => {
    const seed = index * 71 + (emoji ? 307 : 113);
    const trailLength = emoji ? 2 + Math.floor(_clankerNoise(seed + 3) * 3) : 5 + Math.floor(_clankerNoise(seed + 3) * 9);
    const x = ((index + .5) / streamCount) * width + (_clankerNoise(seed + 5) - .5) * cell * (.8 + controls.spread * .2);
    const rareNoise = _clankerNoise(seed + 59);
    const composed = composeRainDirection({
      rainDown: controls.rainDown,
      reverseChance: controls.reverseChance,
      rareUpward: controls.rareUpward,
      noise: _clankerNoise(seed + 31),
      rareNoise,
    });
    const direction = composed.direction;
    const spawn = spawnPosition({
      direction,
      rare: composed.rare,
      waves: controls.waves,
      width,
      height,
      radius: 0,
      entryNoise: _clankerNoise(seed + 7),
      lateralNoise: 0.5,
      spread: controls.spread,
      cell,
      initialX: x,
    });
    // Non-rare random placement still uses the full-height scatter unless waves.
    const y = controls.waves || composed.rare
      ? spawn.y
      : direction > 0
        ? _clankerNoise(seed + 7) * height
        : height - _clankerNoise(seed + 7) * height;
    const initialSpeed = (emoji ? 44 : 74) * (0.72 + _clankerNoise(seed + 11) * .72) * Math.max(.65, size) * controls.speed;
    const chars = Array.from({ length: trailLength + 6 }, (_, charIndex) => pickChar(seed, charIndex * 13) || glyphPool[0]);
    return {
      x,
      y,
      initialX: x,
      initialSpeed,
      speed: initialSpeed,
      direction,
      rare: composed.rare,
      lateral: 0,
      trailLength,
      chars,
      charIndex: Math.floor(_clankerNoise(seed + 19) * chars.length),
      lastCell: Math.floor(y / glyphSize),
      lastHitAt: -Infinity,
      // Junction's splash assigns a discrete behind layer. Behind drops are
      // dimmed and pass through; only foreground drops use the light shove.
      behind: _clankerNoise(seed + 53) < .34,
      color: index % 6,
      colorNoise: _clankerNoise(seed + 37),
      opacity: minOpacity + _clankerNoise(seed + 23) * (maxOpacity - minOpacity),
      scale: Math.max(.45, Math.min(2.4, 1 + (_clankerNoise(seed + 41) * 2 - 1) * controls.sizeVariance)),
      flickerCycle: -1,
      flickerCharIndex: 0,
      resetCount: 0,
      seed,
      phase: _clankerNoise(seed + 29) * Math.PI * 2,
    };
  });
  return {
    mode,
    glyphSize,
    cell,
    streams,
    obstacleTime: -Infinity,
    obstacles: [],
    lastTime: null,
  };
}

function _clankerCodeRainObstacles(scene, time, field = null) {
  // Shared obstacle-field helper: cached painted-alpha field, refreshed on
  // geometry/style/scroll/resize rather than an expensive DOM scan per frame.
  if (field) {
    scene.obstacles = field.obstacles;
    scene.obstacleTime = time;
    return scene.obstacles;
  }
  if (time - scene.obstacleTime < 110) return scene.obstacles;
  scene.obstacleTime = time;
  scene.obstacles = [];
  return scene.obstacles;
}

function _clankerCodeRainHit(stream, x, y, radius, obstacles) {
  if (stream.behind) return null;
  return obstacles.find(rect => x + radius > rect.left && x - radius < rect.right && y + radius > rect.top && y - radius < rect.bottom) || null;
}

function _resetClankerCodeRainStream(stream, scene, controls, width, height, radius) {
  stream.resetCount += 1;
  const rareNoise = _clankerNoise(stream.seed + stream.resetCount * 59);
  const composed = composeRainDirection({
    rainDown: controls.rainDown,
    reverseChance: controls.reverseChance,
    rareUpward: controls.rareUpward,
    noise: _clankerNoise(stream.seed + stream.resetCount * 31),
    rareNoise,
  });
  stream.direction = composed.direction;
  stream.rare = composed.rare;
  const spawn = spawnPosition({
    direction: stream.direction,
    rare: composed.rare,
    waves: controls.waves,
    width,
    height,
    radius,
    entryNoise: _clankerNoise(stream.seed + stream.resetCount * 43),
    lateralNoise: _clankerNoise(stream.seed + stream.resetCount * 47),
    spread: controls.spread,
    cell: scene.cell,
    initialX: stream.initialX,
  });
  stream.y = spawn.y;
  stream.x = spawn.x;
  stream.speed = stream.initialSpeed;
  stream.lateral = 0;
  stream.lastCell = Math.floor(stream.y / scene.glyphSize);
}

function _advanceClankerCodeRainStream(stream, scene, controls, time, delta, width, height, obstacles) {
  const radius = scene.glyphSize * stream.scale * (scene.mode === 'emoji' ? .42 : .5) * (1 + controls.collisionForce * .06);
  const nextX = stream.x + stream.lateral * delta;
  const nextY = stream.y + stream.speed * stream.direction * delta;
  const hit = time - stream.lastHitAt > 130 ? _clankerCodeRainHit(stream, nextX, nextY, radius, obstacles) : null;
  if (hit) {
    // Junction collision is light: preserve nextY/vy/direction and sidestep
    // along the obstacle edge. The .04 term is intentional and remains when
    // both sliders are 0.
    const response = resolveCollision({
      x: nextX,
      y: nextY,
      radius,
      obstacle: hit,
      glyphSize: scene.glyphSize,
      scale: stream.scale,
      collisionForce: controls.collisionForce,
      bounce: controls.bounce,
      random: _clankerNoise(stream.seed + Math.floor(time / 160) * 17),
      behind: stream.behind,
      lastHitAt: stream.lastHitAt,
      time,
    });
    if (response.hit) {
      stream.x = response.x;
      stream.y = response.y;
      stream.lateral = response.lateral;
      stream.lastHitAt = time;
    } else {
      stream.x = nextX;
      stream.y = nextY;
    }
  } else {
    stream.x = nextX;
    stream.y = nextY;
    stream.speed += (stream.initialSpeed - stream.speed) * Math.min(1, delta * 2.2);
    stream.speed += stream.initialSpeed * (controls.gravity - .2) * delta * .35;
    stream.speed = Math.max(stream.initialSpeed * .3, Math.min(stream.initialSpeed * 1.8, stream.speed));
    stream.lateral *= Math.pow(.04, delta);
  }
  const currentCell = Math.floor(stream.y / scene.glyphSize);
  if (currentCell !== stream.lastCell) {
    stream.charIndex = (stream.charIndex + Math.abs(currentCell - stream.lastCell)) % stream.chars.length;
    stream.lastCell = currentCell;
  }
  const flickerPeriod = 160 + (1 - controls.flicker) * 440;
  const flickerCycle = Math.floor(time / flickerPeriod);
  if (flickerCycle !== stream.flickerCycle) {
    stream.flickerCycle = flickerCycle;
    stream.flickerCharIndex = _clankerNoise(stream.seed + flickerCycle * 19) < controls.flicker
      ? Math.floor(_clankerNoise(stream.seed + flickerCycle * 23) * stream.chars.length)
      : stream.charIndex;
  }
  const travel = scene.glyphSize * stream.scale * (stream.trailLength + 2);
  if (stream.direction > 0 && stream.y - travel > height + radius) {
    _resetClankerCodeRainStream(stream, scene, controls, width, height, radius);
  } else if (stream.direction < 0 && stream.y + travel < -radius) {
    _resetClankerCodeRainStream(stream, scene, controls, width, height, radius);
  }
  stream.x = Math.max(-scene.cell, Math.min(width + scene.cell, stream.x));
}

function _drawClankerCodeRain(batch, { width, height, time, scene, intensity, size, colors, outline, reduced, rain, field, font }) {
  const controls = _clankerCodeRainControls(scene.mode || rain.mode);
  const previousTime = rain.lastTime;
  rain.lastTime = time;
  const delta = reduced || previousTime === null ? 0 : Math.min(48, Math.max(0, time - previousTime)) / 1000;
  const obstacles = _clankerCodeRainObstacles(rain, time, field);
  // Match Junction's compositing order: behind rain first, foreground rain
  // second. The Open Clank canvas remains behind arbitrary DOM by design.
  const streams = rain.streams.slice().sort((a, b) => Number(b.behind) - Number(a.behind));
  for (const stream of streams) {
    if (delta) _advanceClankerCodeRainStream(stream, rain, controls, time, delta, width, height, obstacles);
    const glyphStep = rain.glyphSize * stream.scale * .92;
    const charStart = stream.flickerCharIndex || stream.charIndex;
    const colorOffset = Math.floor(stream.colorNoise * colors.length * controls.colorVariance);
    const streamColor = (stream.color + colorOffset) % colors.length;
    for (let index = 0; index <= stream.trailLength; index += 1) {
      const y = stream.y - stream.direction * index * glyphStep;
      if (y < -rain.glyphSize * 2 || y > height + rain.glyphSize * 2 || stream.x < -rain.glyphSize || stream.x > width + rain.glyphSize) continue;
      const tailAlpha = (1 - index / (stream.trailLength + 1)) * stream.opacity * (stream.behind ? .66 : 1);
      // Head-glyph glow (pre-S24 shadowBlur): only the leading glyph glows.
      const glow = index === 0
        ? { color: colors[streamColor], blur: ((rain.mode === 'emoji' ? 8 : 5) * size) }
        : null;
      batch.glyph(
        stream.chars[(charStart + index) % stream.chars.length],
        stream.x,
        y,
        rain.glyphSize * stream.scale,
        400,
        colors[(streamColor + index) % colors.length],
        Math.min(1, intensity * tailAlpha * (index === 0 ? 1 : .72)),
        'center',
        'middle',
        font,
        glow,
      );
    }
  }
  return outline;
}

function _mountClankerCodeRain({ id, bodyClass, mode, getSceneKey = null }) {
  if (document.getElementById(id)) return;
  const host = document.getElementById('chat-container') || document.body;
  const chatPane = host.id === 'chat-container';
  const canvas = document.createElement('canvas');
  canvas.id = id;
  canvas.style.cssText = chatPane
    ? 'position:absolute;left:0;top:0;pointer-events:none;z-index:-1;'
    : 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  canvas.setAttribute('aria-hidden', 'true');
  if (chatPane) host.classList.add('background-effect-host');
  host.prepend(canvas);

  // ONE scene owner via the graphics substrate consumer. theme.js must not
  // also run _runBackgroundCanvas for this canvas (hex: duplicate loop = flicker).
  const field = createObstacleField({
    selectors: _CLANKER_CODE_RAIN_COLLISION_SELECTORS,
    minAlpha: PAINTED_ALPHA_THRESHOLD,
    cacheMs: 110,
    win: window,
    root: document,
  });

  let width = 0;
  let height = 0;
  let dpr = 1;
  let topologyKey = '';

  function updateCanvasLayout() {
    if (!chatPane) return;
    const rect = host.getBoundingClientRect();
    canvas.style.left = `${-Math.round(rect.left)}px`;
    canvas.style.top = `${-Math.round(rect.top)}px`;
    canvas.style.width = `${window.innerWidth}px`;
    canvas.style.height = `${window.innerHeight}px`;
  }

  function measure() {
    const rect = host.getBoundingClientRect();
    const nextWidth = chatPane ? Math.max(1, window.innerWidth) : Math.max(1, Math.round(rect.width));
    const nextHeight = chatPane ? Math.max(1, window.innerHeight) : Math.max(1, Math.round(rect.height));
    const nextDpr = Math.min(window.devicePixelRatio || 1, 2);
    return { nextWidth, nextHeight, nextDpr };
  }

  function ensureRain(scene, nextWidth, nextHeight, nextDpr) {
    const size = _getEffectSize();
    const key = typeof getSceneKey === 'function'
      ? `${nextWidth}:${nextHeight}:${nextDpr}:${size}:${getSceneKey()}`
      : _clankerCodeRainTopologyKey(mode, nextWidth, nextHeight, size);
    const existing = scene.documentState.rain;
    if (existing && topologyKey === key && existing.width === nextWidth && existing.height === nextHeight) {
      return existing;
    }
    const rain = _buildClankerCodeRainScene({
      width: nextWidth,
      height: nextHeight,
      size,
      mode,
    });
    rain.width = nextWidth;
    rain.height = nextHeight;
    topologyKey = key;
    scene.resetTopology({ documentState: { rain } });
    return rain;
  }

  const consumer = createGraphicsConsumer({
    canvas,
    id,
    draw: ({ backend, batch, scene, time, reduced }) => {
      if (!canvas.isConnected || !document.body.classList.contains(bodyClass)) {
        return;
      }
      const measured = measure();
      const geometryChanged = width !== measured.nextWidth || height !== measured.nextHeight || dpr !== measured.nextDpr;
      width = measured.nextWidth;
      height = measured.nextHeight;
      dpr = measured.nextDpr;
      updateCanvasLayout();
      if (geometryChanged) {
        consumer.resize(width, height, dpr);
        field.invalidate();
      }
      const config = _readClankerEffectConfig(true);
      const rain = ensureRain(scene, width, height, dpr);
      // Test/diagnostics seam: expose current scene state without a second owner.
      canvas.__backgroundScene = rain;
      const font = rain.mode === 'emoji' ? _CLANKER_EMOJI_FONT : "'Liga Comic Mono', 'Fira Code', monospace";
      _drawClankerCodeRain(batch, {
        width,
        height,
        time,
        scene,
        rain,
        field,
        font,
        intensity: config.intensity,
        size: config.size,
        colors: config.colors,
        outline: config.outline,
        reduced,
      });
    },
    allowWebGL2: true,
    win: window,
    doc: document,
    ownerHost: window,
  });

  // Reconcile the substrate owner with the theme background owner key so
  // applyBgPattern/dispose see exactly one running owner (S23 nit N6).
  const bodyWatcher = new MutationObserver(() => {
    if (!document.body.classList.contains(bodyClass)) dispose();
  });
  bodyWatcher.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  const resizeObserver = typeof ResizeObserver === 'function'
    ? new ResizeObserver(() => {
      field.invalidate();
      consumer.invalidate();
    })
    : null;
  if (resizeObserver && chatPane) resizeObserver.observe(host);
  const handleWindowResize = () => {
    field.invalidate();
    consumer.invalidate();
  };
  window.addEventListener('resize', handleWindowResize, { passive: true });

  function dispose() {
    try { bodyWatcher.disconnect(); } catch (_) { /* already gone */ }
    if (resizeObserver) resizeObserver.disconnect();
    window.removeEventListener('resize', handleWindowResize);
    field.dispose();
    consumer.dispose();
    if (_activeBackgroundEffectDispose === dispose) _activeBackgroundEffectDispose = null;
    if (window[_BACKGROUND_OWNER_KEY] === dispose) window[_BACKGROUND_OWNER_KEY] = null;
    if (chatPane) host.classList.remove('background-effect-host');
    canvas.remove();
  }

  const activeDispose = window[_BACKGROUND_OWNER_KEY] || _activeBackgroundEffectDispose;
  if (activeDispose) activeDispose();
  _activeBackgroundEffectDispose = dispose;
  window[_BACKGROUND_OWNER_KEY] = dispose;
  canvas.dataset.backgroundEffectCanvas = 'true';
  canvas.__disposeEffect = dispose;

  // Diagnostics seam matching _mountClankerEffect: one paint without a second loop.
  canvas.__backgroundPaint = () => {
    const measured = measure();
    width = measured.nextWidth;
    height = measured.nextHeight;
    dpr = measured.nextDpr;
    consumer.resize(width, height, dpr);
    field.invalidate();
    consumer.invalidate();
  };

  const start = measure();
  width = start.nextWidth;
  height = start.nextHeight;
  dpr = start.nextDpr;
  updateCanvasLayout();
  consumer.resize(width, height, dpr);
}

function _initClankerMatrixRain() {
  _mountClankerCodeRain({
    id: 'clanker-matrix-rain-canvas',
    bodyClass: 'bg-pattern-clanker-matrix-rain',
    mode: 'matrix',
    getSceneKey: () => _clankerCodeRainTopologyKey('matrix', 0, 0, 0),
  });
}

function _initClankerEmojiRain() {
  _mountClankerCodeRain({
    id: 'clanker-emoji-rain-canvas',
    bodyClass: 'bg-pattern-clanker-emoji-rain',
    mode: 'emoji',
    getSceneKey: () => _clankerCodeRainTopologyKey('emoji', 0, 0, 0),
  });
}

// ── Synapse background effect ──
// Stable organic signal mesh with a few bounded pulses.
function _initSynapse() {
  if (document.getElementById('synapse-canvas')) return;
  const canvas = document.createElement('canvas');
  canvas.id = 'synapse-canvas';
  canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  // Decorative background effect — hide from assistive tech so screen readers
  // don't announce an empty canvas and axe's "region" rule doesn't flag it.
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);
  const ctx = canvas.getContext('2d');
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  const GRID = 92;
  const MAX_PULSES = 18;
  const TRAIL_LEN = 42;

  let W, H, cols, rows, pulses = [], neurons = [], edges = [];

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    // T18: recompute backing scale on every resize so zoom/display changes are
    // picked up instead of freezing the DPR captured at init.
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cols = Math.ceil(W / GRID); rows = Math.ceil(H / GRID);
    neurons = Array.from({ length: Math.max(48, Math.ceil(W * H / 18000)) }, (_, index) => {
      const seed = index * 31 + 7;
      return { x:_clankerNoise(seed) * W, y:_clankerNoise(seed + 5) * H, color:index % 6,
        radius:1.2 + _clankerNoise(seed + 11) * 2.2, phase:_clankerNoise(seed + 17) * Math.PI * 2 };
    });
    edges = [];
    neurons.forEach((node, index) => {
      const nearby = neurons.slice(index + 1).map(other => ({ other, distance:Math.hypot(node.x - other.x, node.y - other.y) }))
        .filter(item => item.distance < GRID * 1.7).sort((a, b) => a.distance - b.distance).slice(0, 2);
      nearby.forEach(item => edges.push({ a:node, b:item.other, color:(index + edges.length) % 6 }));
    });
    pulses = [];
    for (let index = 0; index < MAX_PULSES; index += 1) {
      spawnPulse(index);
    }
  }

  function spawnPulse(index) {
    const seed = index * 43 + 19;
    const speed = .025 + _clankerNoise(seed + 3) * .035;
    if (_clankerNoise(seed + 7) > .5) {
      const row = Math.floor(_clankerNoise(seed + 11) * (rows + 1));
      pulses.push({ horizontal:true, anchor:row * GRID, speed, phase:_clankerNoise(seed + 13), color:index % 6 });
    } else {
      const col = Math.floor(_clankerNoise(seed + 11) * (cols + 1));
      pulses.push({ horizontal:false, anchor:col * GRID, speed, phase:_clankerNoise(seed + 13), color:index % 6 });
    }
  }

  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, size } = _readClankerEffectConfig();
    edges.forEach(edge => {
      ctx.beginPath(); ctx.moveTo(edge.a.x, edge.a.y); ctx.lineTo(edge.b.x, edge.b.y);
      ctx.strokeStyle = colors[edge.color]; ctx.lineWidth = .7 * size; ctx.globalAlpha = .12; ctx.stroke();
    });
    neurons.forEach(node => {
      const pulse = .72 + Math.sin(time / 2600 + node.phase) * .18;
      ctx.beginPath(); ctx.arc(node.x, node.y, node.radius * size * pulse, 0, Math.PI * 2);
      ctx.fillStyle = colors[node.color]; ctx.globalAlpha = .34; ctx.fill();
    });
    pulses.forEach(p => {
      const span = (p.horizontal ? W : H) + TRAIL_LEN * 2;
      const head = (time * p.speed + p.phase * span) % span - TRAIL_LEN;
      const x = p.horizontal ? head : p.anchor;
      const y = p.horizontal ? p.anchor : head;
      const tx = x - (p.horizontal ? TRAIL_LEN : 0);
      const ty = y - (p.horizontal ? 0 : TRAIL_LEN);
      const grad = ctx.createLinearGradient(tx, ty, x, y);
      grad.addColorStop(0, 'transparent');
      grad.addColorStop(1, colors[p.color]);
      ctx.strokeStyle = grad;
      ctx.globalAlpha = .56;
      ctx.lineWidth = 1.4 * size;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.globalAlpha = .9;
      ctx.fillStyle = colors[p.color];
      ctx.beginPath();
      ctx.arc(x, y, 2 * size, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.globalAlpha = 1;
  }
  _runBackgroundCanvas({ canvas, bodyClass: 'bg-pattern-synapse', resize, paint: draw });
}

// ── Rain — thin vertical streaks falling ──
function _initRain() {
  if (document.getElementById('rain-canvas')) return;
  const canvas = document.createElement('canvas');
  canvas.id = 'rain-canvas';
  canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  // Decorative background effect — hide from assistive tech so screen readers
  // don't announce an empty canvas and axe's "region" rule doesn't flag it.
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);
  const ctx = canvas.getContext('2d');
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H;
  let drops = [];
  const MAX_DROPS = 130;

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    // T18: recompute backing scale on every resize so zoom/display changes are
    // picked up instead of freezing the DPR captured at init.
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drops = Array.from({ length: MAX_DROPS }, (_, index) => {
      const seed = index * 37 + 5;
      return { x:_clankerNoise(seed) * W, len:20 + _clankerNoise(seed + 3) * 52,
        speed:.035 + _clankerNoise(seed + 7) * .07, phase:_clankerNoise(seed + 11),
        alpha:.15 + _clankerNoise(seed + 17) * .24, color:index % 6 };
    });
  }

  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, size } = _readClankerEffectConfig();
    drops.forEach(d => {
      const effLen = d.len * size;
      const span = H + effLen * 2;
      const y = ((d.phase + time / 1000 * d.speed) % 1) * span - effLen;
      const grad = ctx.createLinearGradient(d.x, y - effLen, d.x, y);
      grad.addColorStop(0, 'transparent');
      grad.addColorStop(1, colors[d.color]);
      ctx.strokeStyle = grad;
      ctx.globalAlpha = d.alpha;
      ctx.lineWidth = 1.2 * Math.min(2, Math.max(.6, size));
      ctx.beginPath();
      ctx.moveTo(d.x, y - effLen);
      ctx.lineTo(d.x, y);
      ctx.stroke();
    });
    ctx.globalAlpha = 1;
  }
  _runBackgroundCanvas({ canvas, bodyClass: 'bg-pattern-rain', resize, paint: draw });
}

// ── Constellations — static dots that slowly form/dissolve connecting lines ──
function _initConstellations() {
  if (document.getElementById('constellations-canvas')) return;
  const canvas = document.createElement('canvas');
  canvas.id = 'constellations-canvas';
  canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  // Decorative background effect — hide from assistive tech so screen readers
  // don't announce an empty canvas and axe's "region" rule doesn't flag it.
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);
  const ctx = canvas.getContext('2d');
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H;
  const STAR_COUNT = 88;
  const CONNECT_DIST = 148;
  let stars = [];

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    // T18: recompute backing scale on every resize so zoom/display changes are
    // picked up instead of freezing the DPR captured at init.
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (stars.length === 0) initStars();
  }

  function initStars() {
    stars = [];
    for (let i = 0; i < STAR_COUNT; i++) {
      const seed = i * 41 + 3;
      stars.push({
        x: _clankerNoise(seed) * W, y: _clankerNoise(seed + 5) * H,
        driftX: 2 + _clankerNoise(seed + 7) * 8,
        driftY: 2 + _clankerNoise(seed + 11) * 8,
        r: .8 + _clankerNoise(seed + 13) * 1.8,
        phase: _clankerNoise(seed + 17) * Math.PI * 2,
        color: i % 6,
      });
    }
  }

  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, size } = _readClankerEffectConfig();
    const points = stars.map(star => ({ ...star,
      drawX: star.x + Math.sin(time / 8500 + star.phase) * star.driftX,
      drawY: star.y + Math.cos(time / 9800 + star.phase) * star.driftY }));

    ctx.lineWidth = .6 * size;
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const dx = points[i].drawX - points[j].drawX;
        const dy = points[i].drawY - points[j].drawY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < CONNECT_DIST) {
          ctx.strokeStyle = colors[(points[i].color + points[j].color) % colors.length];
          ctx.globalAlpha = (1 - dist / CONNECT_DIST) * .16;
          ctx.beginPath();
          ctx.moveTo(points[i].drawX, points[i].drawY);
          ctx.lineTo(points[j].drawX, points[j].drawY);
          ctx.stroke();
        }
      }
    }

    for (const s of points) {
      const twinkle = .5 + .5 * Math.sin(time / 1700 + s.phase);
      ctx.fillStyle = colors[s.color];
      ctx.globalAlpha = (.18 + twinkle * .34);
      ctx.beginPath();
      ctx.arc(s.drawX, s.drawY, s.r * size, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }
  _runBackgroundCanvas({
    canvas,
    bodyClass: 'bg-pattern-constellations',
    resize: () => { resize(); initStars(); },
    paint: draw,
  });
}

// ── Noise helper for Perlin effects ──
function _bgNoise2d(x, y) { const n = Math.sin(x * 12.9898 + y * 78.233) * 43758.5453; return n - Math.floor(n); }
function _bgSmoothNoise(x, y) {
  const ix = Math.floor(x), iy = Math.floor(y), fx = x - ix, fy = y - iy;
  const a = _bgNoise2d(ix, iy), b = _bgNoise2d(ix + 1, iy), cc = _bgNoise2d(ix, iy + 1), d = _bgNoise2d(ix + 1, iy + 1);
  const ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
  return a + (b - a) * ux + (cc - a) * uy + (a - b - cc + d) * ux * uy;
}

// ── Perlin Flow — colored particle streams ──
function _initPerlinFlow() {
  if (document.getElementById('perlin-flow-canvas')) return;
  const canvas = document.createElement('canvas');
  canvas.id = 'perlin-flow-canvas';
  canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  // Decorative background effect — hide from assistive tech so screen readers
  // don't announce an empty canvas and axe's "region" rule doesn't flag it.
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);
  const ctx = canvas.getContext('2d');
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H, streams = [];
  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    // T18: recompute backing scale on every resize so zoom/display changes are
    // picked up instead of freezing the DPR captured at init.
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    streams = Array.from({ length: Math.max(54, Math.ceil(W * H / 24000)) }, (_, index) => {
      const seed = index * 53 + 7;
      let x = _clankerNoise(seed) * W;
      let y = _clankerNoise(seed + 5) * H;
      const points = [{ x, y }];
      for (let step = 0; step < 42; step += 1) {
        const noise = _bgSmoothNoise(x * .0038 + index * .17, y * .0038 + 80);
        const angle = noise * Math.PI * 4 + index * .07;
        x += Math.cos(angle) * 11;
        y += Math.sin(angle) * 11;
        if (x < -12 || x > W + 12 || y < -12 || y > H + 12) break;
        points.push({ x, y });
      }
      return { points, color:index % 6, phase:_clankerNoise(seed + 11) };
    }).filter(stream => stream.points.length > 4);
  }
  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, size } = _readClankerEffectConfig();
    streams.forEach((stream, index) => {
      ctx.beginPath();
      stream.points.forEach((point, pointIndex) => pointIndex ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
      ctx.strokeStyle = colors[stream.color];
      ctx.lineWidth = Math.max(.65, size * .85);
      ctx.globalAlpha = .16;
      ctx.stroke();
      if (index % 6 !== 0) return;
      const head = _pointOnPolyline(stream.points, time / (10500 + index * 37) + stream.phase);
      ctx.beginPath(); ctx.arc(head.x, head.y, 2.1 * size, 0, Math.PI * 2);
      ctx.fillStyle = colors[(stream.color + 2) % colors.length];
      ctx.globalAlpha = .8; ctx.fill();
    });
    ctx.globalAlpha = 1;
  }
  _runBackgroundCanvas({ canvas, bodyClass: 'bg-pattern-perlin-flow', resize, paint: draw });
}

// ── Petals — gentle falling flower petals ──
function _initPetals() {
  if (document.getElementById('petals-canvas')) return;
  const canvas = document.createElement('canvas');
  canvas.id = 'petals-canvas';
  canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  // Decorative background effect — hide from assistive tech so screen readers
  // don't announce an empty canvas and axe's "region" rule doesn't flag it.
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);
  const ctx = canvas.getContext('2d');
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H;
  let petals = [];
  function makePetal(index) {
    const seed = index * 47 + 3;
    return {
      x:_clankerNoise(seed) * W,
      size:3 + _clankerNoise(seed + 5) * 5,
      rot:_clankerNoise(seed + 7) * Math.PI * 2,
      vr:(-_clankerNoise(seed + 11) + .5) * .0012,
      speed:.018 + _clankerNoise(seed + 13) * .028,
      phase:_clankerNoise(seed + 17),
      drift:_clankerNoise(seed + 19) * Math.PI * 2,
      wobble:8 + _clankerNoise(seed + 23) * 22,
      color:index % 6,
    };
  }
  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    // T18: recompute backing scale on every resize so zoom/display changes are
    // picked up instead of freezing the DPR captured at init.
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    petals = Array.from({ length:64 }, (_, index) => makePetal(index));
  }
  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, size:sz } = _readClankerEffectConfig();
    petals.forEach(p => {
      const progress = (p.phase + time / 1000 * p.speed) % 1;
      const y = progress * (H + 40) - 20;
      const x = p.x + Math.sin(time / 2600 + p.drift) * p.wobble;
      ctx.save(); ctx.translate(x, y); ctx.rotate(p.rot + time * p.vr);
      ctx.globalAlpha = .24;
      ctx.fillStyle = colors[p.color];
      ctx.beginPath(); ctx.ellipse(-p.size * 0.2 * sz, 0, p.size * 0.6 * sz, p.size * 0.3 * sz, 0.3, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = .16;
      ctx.beginPath(); ctx.ellipse(p.size * 0.2 * sz, 0, p.size * 0.6 * sz, p.size * 0.3 * sz, -0.3, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    });
    ctx.globalAlpha = 1;
  }
  _runBackgroundCanvas({ canvas, bodyClass: 'bg-pattern-petals', resize, paint: draw });
}

// ── Sparkles — slow-glow star shapes ──
function _initSparkles() {
  if (document.getElementById('sparkles-canvas')) return;
  const canvas = document.createElement('canvas');
  canvas.id = 'sparkles-canvas';
  canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  // Decorative background effect — hide from assistive tech so screen readers
  // don't announce an empty canvas and axe's "region" rule doesn't flag it.
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);
  const ctx = canvas.getContext('2d');
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H;
  let sparkles = [];
  function makeSpark(index) {
    const seed = index * 43 + 13;
    return { x:_clankerNoise(seed) * W, y:_clankerNoise(seed + 5) * H,
      size:2 + _clankerNoise(seed + 7) * 5, phase:_clankerNoise(seed + 11) * Math.PI * 2,
      speed:.00045 + _clankerNoise(seed + 17) * .00085, life:.45 + _clankerNoise(seed + 19) * .55,
      color:index % 6, bright:index % 13 === 0 };
  }
  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    // T18: recompute backing scale on every resize so zoom/display changes are
    // picked up instead of freezing the DPR captured at init.
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    sparkles = Array.from({ length:96 }, (_, index) => makeSpark(index));
  }
  function drawStar(x, y, r, c, alpha) {
    ctx.save(); ctx.translate(x, y); ctx.fillStyle = c; ctx.globalAlpha = alpha;
    // 4-point star
    ctx.beginPath();
    ctx.moveTo(0, -r); ctx.quadraticCurveTo(r * 0.15, -r * 0.15, r, 0);
    ctx.quadraticCurveTo(r * 0.15, r * 0.15, 0, r);
    ctx.quadraticCurveTo(-r * 0.15, r * 0.15, -r, 0);
    ctx.quadraticCurveTo(-r * 0.15, -r * 0.15, 0, -r);
    ctx.fill();
    ctx.restore();
  }
  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, size:sizeMult } = _readClankerEffectConfig();
    sparkles.forEach(s => {
      const glow = .5 + .5 * Math.sin(time * s.speed + s.phase);
      const alpha = (.06 + glow * (s.bright ? .42 : .2)) * s.life;
      const scale = 0.72 + glow * 0.28;
      drawStar(s.x, s.y, s.size * scale * sizeMult, colors[s.color], alpha);
    });
    ctx.globalAlpha = 1;
  }
  _runBackgroundCanvas({ canvas, bodyClass: 'bg-pattern-sparkles', resize, paint: draw });
}

// ── Embers — warm particles rising with a persistent glow ──
function _initEmbers() {
  if (document.getElementById('embers-canvas')) return;
  const canvas = document.createElement('canvas');
  canvas.id = 'embers-canvas';
  canvas.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  // Decorative background effect — hide from assistive tech so screen readers
  // don't announce an empty canvas and axe's "region" rule doesn't flag it.
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);
  const ctx = canvas.getContext('2d');
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H;
  let embers = [];
  function makeEmber(index) {
    const seed = index * 59 + 7;
    return {
      x:_clankerNoise(seed) * W,
      phase:_clankerNoise(seed + 5),
      speed:.012 + _clankerNoise(seed + 11) * .03,
      r:.7 + _clankerNoise(seed + 13) * 1.8,
      wobble:_clankerNoise(seed + 17) * Math.PI * 2,
      drift:5 + _clankerNoise(seed + 19) * 20,
      color:[1, 4, 3][index % 3],
      bright:index % 11 === 0,
    };
  }
  function resize() {
    W = window.innerWidth; H = window.innerHeight;
    // T18: recompute backing scale on every resize so zoom/display changes are
    // picked up instead of freezing the DPR captured at init.
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    embers = Array.from({ length:96 }, (_, index) => makeEmber(index));
  }
  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, size:sz } = _readClankerEffectConfig();
    ctx.globalCompositeOperation = 'lighter';
    embers.forEach(e => {
      const lifeRatio = (e.phase + time / 1000 * e.speed) % 1;
      const fade = Math.min(1, lifeRatio * 7, (1 - lifeRatio) * 7);
      const x = e.x + Math.sin(time / 1800 + e.wobble) * e.drift;
      const y = H + 18 - lifeRatio * (H + 36);
      const r = e.r * sz;
      ctx.fillStyle = colors[e.color];
      ctx.globalAlpha = fade * (e.bright ? .82 : .38);
      ctx.shadowColor = colors[e.color];
      ctx.shadowBlur = (e.bright ? 10 : 4) * sz;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
    });
    ctx.shadowBlur = 0;
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
  }
  _runBackgroundCanvas({ canvas, bodyClass: 'bg-pattern-embers', resize, paint: draw });
}

const themeModule = { initThemeUI, togglePopup, closePopup, makeDraggable,
                       THEMES, applyTheme, applyThemeIdentity, applyColors, applyFontDensity, applyBgPattern,
                       applyBgEffectColor, applyBgEffectIntensity, applyBgEffectSize,
                       applyFrostedGlass,
                       save, getSaved, saveCustomTheme, deleteCustomTheme,
                       getCustomThemes, normalizeThemeSnapshot, themeStorageKey };

export default themeModule;

// Init on DOM ready, with server-side sync fallback
async function _initWithSync() {
  initThemeUI();
  // Auth is intentionally resolved here as well as by init.js. This closes
  // the window where an account switch could leave the prior account's theme
  // visible while an old response is still in flight.
  // On the full shell, init.js owns this lookup and dispatches the two legacy
  // compatibility events. Await its shared promise so a fast theme module
  // cannot start a second hydration/network pair before those events arrive.
  if (window.__odysseusAuthContextPromise) {
    const context = await window.__odysseusAuthContextPromise;
    if (context?.username || context?.accountId) return;
  }
  try {
    const response = await fetch('/api/auth/status', { credentials: 'same-origin' });
    const auth = response.ok ? await response.json() : null;
    if (auth?.username || auth?.account_id) {
      await _requestAccountHydration({ username: auth.username, accountId: auth.account_id });
      return;
    }
  } catch (_) { /* anonymous/offline boot uses the local snapshot */ }
  if (!getSaved()) {
    const serverTheme = await _loadFromServer();
    if (serverTheme) _applySnapshot(serverTheme, false);
  }
}

function _startThemeInit() {
  if (_themeInitPromise) return _themeInitPromise;
  _themeInitPromise = _initWithSync();
  return _themeInitPromise;
}

if (typeof document !== 'undefined') {
  document.addEventListener('openclank:auth-user-ready', event => {
    _requestAccountHydration(event.detail || {});
  });
  document.addEventListener('openclank:auth-context-changed', event => {
    _requestAccountHydration(event.detail || {});
  });
}

if (typeof document !== 'undefined' && document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => _startThemeInit(), { once: true });
} else if (typeof document !== 'undefined') {
  _startThemeInit();
}
