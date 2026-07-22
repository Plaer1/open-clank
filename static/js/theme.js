// Theme system — preset themes + custom color editing, stored in localStorage
// ES6 module

import Storage from './storage.js';
import uiModule from './ui.js';
import { initColorPickers, attachColorPicker } from './colorPicker.js';
import { hexToRgb } from './color/hex.js';
import { makeWindowDraggable } from './windowDrag.js';
import { snapModalToZone } from './tileManager.js';

export const THEMES = {
  'clanker-dark': {
    bg:'#191A1E', fg:'#FFF4D6', panel:'#25272C', border:'#555A62', red:'#5A9EF5',
    advanced: { userBubbleBg:'#30333A', aiBubbleBg:'#25272C', bubbleBorder:'#555A62',
                sidebarBg:'#202227', brandColor:'#F6BE48', brandMixTo:'#ED6AB0',
                hamburgerColor:'#FFF4D6', inputBg:'#2B2E34', inputBorder:'#555A62',
                sendBtnBg:'#5A9EF5', sendBtnHover:'#3276D4', codeBg:'#101114',
                codeFg:'#FFF4D6', toggleActive:'#A8DE53' },
  },
  'clanker-light': {
    bg:'#F3EEDB', fg:'#17202A', panel:'#FFF9E7', border:'#26323D', red:'#2469D8',
    advanced: { userBubbleBg:'#DCEAF0', aiBubbleBg:'#FFF9E7', bubbleBorder:'#26323D',
                sidebarBg:'#F4B827', brandColor:'#EC635C', brandMixTo:'#D94B9C',
                hamburgerColor:'#17202A', inputBg:'#FFF9E7', inputBorder:'#26323D',
                sendBtnBg:'#2469D8', sendBtnHover:'#10489D', codeBg:'#111A27',
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

// ── Custom theme persistence ──
function _loadCustomThemes() {
  return Storage.getJSON(CUSTOM_THEMES_KEY, {});
}
function _saveCustomThemes(obj) {
  Storage.setJSON(CUSTOM_THEMES_KEY, obj);
}
export function saveCustomTheme(name, colors, opts) {
  const ct = _loadCustomThemes();
  // Enforce limit — allow overwriting existing, block new past max
  if (!ct[name] && Object.keys(ct).length >= MAX_CUSTOM_THEMES) {
    return 'limit';
  }
  const entry = { ...colors };
  if (opts) {
    if (opts.font) entry.font = opts.font;
    if (opts.density) entry.density = opts.density;
    if (opts.bgPattern) entry.bgPattern = opts.bgPattern;
    if (opts.bgEffectColor) entry.bgEffectColor = opts.bgEffectColor;
    if (opts.bgEffectIntensity !== undefined) entry.bgEffectIntensity = opts.bgEffectIntensity;
    if (opts.bgEffectSize !== undefined) entry.bgEffectSize = opts.bgEffectSize;
    if (opts.frosted !== undefined) entry.frosted = !!opts.frosted;
  }
  ct[name] = entry;
  _saveCustomThemes(ct);
  _syncCustomThemesToServer(ct);
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
  const red = colors.red || '#e06c75';
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
  const s = document.documentElement.style;
  s.setProperty('--bg', colors.bg);
  s.setProperty('--fg', colors.fg);
  s.setProperty('--panel', colors.panel);
  s.setProperty('--border', colors.border);
  if (colors.red) s.setProperty('--red', colors.red);

  // Keep the mobile browser toolbar / status bar matched to the theme bg
  // (same as the early head-script does on first paint).
  const _mtc = document.querySelector('meta[name="theme-color"]');
  if (_mtc && colors.bg) _mtc.setAttribute('content', colors.bg);

  // Derive and apply syntax highlighting colors
  const syn = deriveSyntaxColors(colors);
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
  const adv = colors.advanced || {};
  const defaults = computeAdvancedDefaults(colors);
  for (const { key, css } of ADV_KEYS) {
    s.setProperty(css, adv[key] || defaults[key]);
  }

  // Update favicon to match theme accent color
  _updateFavicon(colors.red || '#e06c75');
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
  'bg-pattern-clanker-radar', 'bg-pattern-clanker-gem-drift', 'bg-pattern-clanker-sweep', 'bg-pattern-clanker-blueprint',
  'bg-pattern-synapse', 'bg-pattern-rain', 'bg-pattern-constellations',
  'bg-pattern-perlin-flow',
  'bg-pattern-petals', 'bg-pattern-sparkles', 'bg-pattern-embers'];
const _CANVAS_PATTERNS = { 'clanker-routefield': _initClankerRoutefield,
  'clanker-kene-weave': _initClankerKeneWeave,
  'clanker-radar': _initClankerRadar,
  'clanker-gem-drift': _initClankerGemDrift,
  synapse: _initSynapse, rain: _initRain, constellations: _initConstellations,
  'perlin-flow': _initPerlinFlow,
  petals: _initPetals, sparkles: _initSparkles, embers: _initEmbers };
const _BACKGROUND_CANVAS_SELECTOR = '[data-background-effect-canvas], [data-clanker-effect-canvas], #synapse-canvas, #rain-canvas, #constellations-canvas, #perlin-flow-canvas, #petals-canvas, #sparkles-canvas, #embers-canvas';
let _activeBackgroundEffectDispose = null;

function _disposeBackgroundEffect() {
  const dispose = _activeBackgroundEffectDispose;
  _activeBackgroundEffectDispose = null;
  if (dispose) dispose();
  document.querySelectorAll(_BACKGROUND_CANVAS_SELECTOR).forEach(canvas => {
    if (typeof canvas.__disposeEffect === 'function') canvas.__disposeEffect();
    else canvas.remove();
  });
}

export function applyBgEffectColor(color) {
  document.documentElement.style.setProperty('--bg-effect-color', color || '');
}

export function applyBgEffectIntensity(v) {
  // v is 0..1. Default 1 (full intensity) when missing.
  const n = (v === undefined || v === null || isNaN(v)) ? 1 : Math.max(0, Math.min(1, Number(v)));
  document.documentElement.style.setProperty('--bg-effect-intensity', String(n));
}

export function applyBgEffectSize(v) {
  // v is a multiplier 0.3..2.5. Default 1 when missing.
  const n = (v === undefined || v === null || isNaN(v)) ? 1 : Math.max(0.2, Math.min(3, Number(v)));
  document.documentElement.style.setProperty('--bg-effect-size', String(n));
  document.documentElement.style.setProperty('--clanker-grid-size', `${Math.round(32 * n)}px`);
  document.documentElement.style.setProperty('--clanker-route-size', `${Math.round(160 * n)}px`);
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

// Patterns where the intensity/size sliders have no visible effect.
const _STATIC_PATTERNS = new Set(['none', 'dots']);

export function applyBgPattern(pattern) {
  const p = pattern || 'none';
  document.body.classList.remove(..._BG_CLASSES);
  _disposeBackgroundEffect();
  if (p !== 'none') document.body.classList.add('bg-pattern-' + p);
  if (_CANVAS_PATTERNS[p]) _CANVAS_PATTERNS[p]();
  // Hide sliders that do nothing on static patterns.
  const hide = _STATIC_PATTERNS.has(p);
  const ig = document.getElementById('theme-bg-intensity-group');
  const sg = document.getElementById('theme-bg-size-group');
  if (ig) ig.style.display = hide ? 'none' : '';
  if (sg) sg.style.display = hide ? 'none' : '';
}

export function getSaved() {
  const obj = Storage.getJSON(LS_KEY, null);
  // Migration: 'chatgpt' preset was renamed to 'gpt'
  if (obj && obj.name === 'chatgpt') obj.name = 'gpt';
  // Migration: 'sakura' preset was renamed to 'ume'
  if (obj && obj.name === 'sakura') obj.name = 'ume';
  // Built-in themes are versioned product presets. Prior Clanker Dark saves
  // should receive the current Toon Command palette and route-field while
  // user-edited palettes continue to live under the separate custom theme.
  if (obj && obj.name === 'clanker-dark') {
    obj.colors = { ...THEMES['clanker-dark'], advanced: { ...THEMES['clanker-dark'].advanced } };
    if (!obj.bgPattern || obj.bgPattern === 'clanker-sweep') obj.bgPattern = THEME_DEFAULT_PATTERN['clanker-dark'];
    if (!obj.bgEffectColor || (typeof obj.bgEffectColor === 'string' && ['#78D4F3', '#5ABCF5'].includes(obj.bgEffectColor.toUpperCase()))) obj.bgEffectColor = THEME_DEFAULT_EFFECT_COLOR['clanker-dark'];
    if (obj.bgEffectIntensity === undefined || obj.bgEffectIntensity === 0.7 || obj.bgEffectIntensity === 0.8) obj.bgEffectIntensity = THEME_DEFAULT_INTENSITY['clanker-dark'];
    Storage.setJSON(LS_KEY, obj);
  }
  return obj;
}

export function save(name, colors, opts) {
  const obj = { name, colors };
  if (opts) {
    if (opts.font && opts.font !== DEFAULT_FONT) obj.font = opts.font;
    if (opts.density && opts.density !== DEFAULT_DENSITY) obj.density = opts.density;
    if (opts.bgPattern && opts.bgPattern !== 'none') obj.bgPattern = opts.bgPattern;
    if (opts.bgEffectColor) obj.bgEffectColor = opts.bgEffectColor;
    if (opts.bgEffectIntensity !== undefined && opts.bgEffectIntensity !== 1) obj.bgEffectIntensity = opts.bgEffectIntensity;
    if (opts.bgEffectSize !== undefined && opts.bgEffectSize !== 1) obj.bgEffectSize = opts.bgEffectSize;
    if (opts.frosted) obj.frosted = true;
  }
  Storage.setJSON(LS_KEY, obj);
  _syncToServer(obj);
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
  document.body.classList.remove('theme-clanker-dark', 'theme-clanker-light');
  if (name === 'clanker-dark' || name === 'clanker-light') {
    document.body.classList.add('theme-' + name);
    document.documentElement.style.setProperty('color-scheme', name === 'clanker-light' ? 'light' : 'dark');
  } else {
    document.documentElement.style.removeProperty('color-scheme');
  }
}

function _getThemeOptions(name, source = {}) {
  const savedPattern = name === 'clanker-dark' && source.bgPattern === 'clanker-sweep'
    ? THEME_DEFAULT_PATTERN[name]
    : source.bgPattern;
  const savedEffectColor = name === 'clanker-dark'
    && (!source.bgEffectColor || (typeof source.bgEffectColor === 'string' && source.bgEffectColor.toUpperCase() === '#78D4F3'))
    ? THEME_DEFAULT_EFFECT_COLOR[name]
    : source.bgEffectColor;
  const savedEffectIntensity = name === 'clanker-dark' && source.bgEffectIntensity === 0.7
    ? THEME_DEFAULT_INTENSITY[name]
    : source.bgEffectIntensity;
  return {
    font: THEME_DEFAULT_FONT[name] || source.font || DEFAULT_FONT,
    density: source.density || DEFAULT_DENSITY,
    bgPattern: savedPattern || THEME_DEFAULT_PATTERN[name] || 'none',
    bgEffectColor: savedEffectColor || THEME_DEFAULT_EFFECT_COLOR[name] || '',
    bgEffectIntensity: savedEffectIntensity !== undefined
      ? savedEffectIntensity
      : (THEME_DEFAULT_INTENSITY[name] !== undefined ? THEME_DEFAULT_INTENSITY[name] : 1),
    bgEffectSize: source.bgEffectSize !== undefined
      ? source.bgEffectSize
      : (THEME_DEFAULT_SIZE[name] !== undefined ? THEME_DEFAULT_SIZE[name] : 1),
    frosted: source.frosted !== undefined ? !!source.frosted : THEME_DEFAULT_FROSTED[name] === true,
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
  const colors = builtIn ? THEMES[name] : (providedColors || custom[name]);
  if (!colors) return null;
  const optionSource = config.storedOptions || (builtIn ? {} : (custom[name] || colors));
  const opts = _getThemeOptions(name, optionSource);
  applyColors(colors);
  applyThemeIdentity(name);
  applyFontDensity(opts.font, opts.density);
  applyBgEffectColor(opts.bgEffectColor);
  applyBgEffectIntensity(opts.bgEffectIntensity);
  applyBgEffectSize(opts.bgEffectSize);
  applyFrostedGlass(opts.frosted);
  applyBgPattern(opts.bgPattern);
  _syncThemeControls(name, colors, opts);
  if (config.persist !== false) save(name, colors, opts);
  return { colors, opts };
}

async function _loadFromServer() {
  try {
    const res = await fetch('/api/prefs/theme', { credentials: 'same-origin' });
    const data = await res.json();
    return data.value || null;
  } catch { return null; }
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
  if (themeTabs) {
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
    const fr = document.getElementById('theme-frosted-toggle');
    if (fr) opts.frosted = !!fr.checked;
    return opts;
  }
  function _saveFull(name, colors) { save(name, colors, _getOpts()); }

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
      // Auto-save: if the active theme is one of the user's custom themes,
      // route changes back into it so renaming/reloading keeps the edits.
      // Otherwise fall back to the transient 'custom' slot (existing behavior).
      const _activeSaved = getSaved();
      const _activeName = _activeSaved && _activeSaved.name;
      const _customMap = _loadCustomThemes();
      if (_activeName && _customMap && _customMap[_activeName]) {
        // Preserve advanced/opts keys that aren't part of basic colors.
        saveCustomTheme(_activeName, colors, {
          font: _activeSaved.font, density: _activeSaved.density,
          bgPattern: _activeSaved.bgPattern, bgEffectColor: _activeSaved.bgEffectColor,
          bgEffectIntensity: _activeSaved.bgEffectIntensity,
          bgEffectSize: _activeSaved.bgEffectSize,
        });
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
      Storage.remove(LS_KEY);
      const colors = THEMES[DEFAULT_THEME];
      applyTheme(DEFAULT_THEME, colors, { persist: false });
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
      // Same auto-save routing as the basic color inputs above — write
      // to the active custom theme if there is one, else fall back to
      // the transient 'custom' slot.
      const _activeSaved = getSaved();
      const _activeName = _activeSaved && _activeSaved.name;
      const _customMap = _loadCustomThemes();
      if (_activeName && _customMap && _customMap[_activeName]) {
        saveCustomTheme(_activeName, base, {
          font: _activeSaved.font, density: _activeSaved.density,
          bgPattern: _activeSaved.bgPattern, bgEffectColor: _activeSaved.bgEffectColor,
          bgEffectIntensity: _activeSaved.bgEffectIntensity,
          bgEffectSize: _activeSaved.bgEffectSize,
        });
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
  const _initFrosted = _initialOptions.frosted;
  applyFontDensity(_initFont, _initDensity);
  applyBgEffectColor(_initEffectColor);
  applyBgEffectIntensity(_initEffectIntensity);
  applyBgEffectSize(_initEffectSize);
  applyFrostedGlass(_initFrosted);
  applyBgPattern(_initPattern);

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
        // Restore saved value after options are populated
        nf.value = _initFont;
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
  if (effectColorPicker) {
    effectColorPicker.value = _initEffectColor || currentColors.fg || '#9cdef2';
    effectColorPicker.addEventListener('input', () => {
      applyBgEffectColor(effectColorPicker.value);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }

  const intensitySlider = document.getElementById('theme-bg-intensity');
  if (intensitySlider) {
    intensitySlider.value = String(Math.round(_initEffectIntensity * 100));
    intensitySlider.addEventListener('input', () => {
      applyBgEffectIntensity(parseFloat(intensitySlider.value) / 100);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }

  const sizeSlider = document.getElementById('theme-bg-size');
  if (sizeSlider) {
    sizeSlider.value = String(Math.round(_initEffectSize * 100));
    sizeSlider.addEventListener('input', () => {
      applyBgEffectSize(parseFloat(sizeSlider.value) / 100);
      const s = getSaved(); if (s) _saveFull(s.name, s.colors);
    });
  }

  const frostedToggle = document.getElementById('theme-frosted-toggle');
  if (frostedToggle) {
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
      const result = saveCustomTheme(slug, colorData, opts);
      if (result === 'limit') { saveError.textContent = 'Max ' + MAX_CUSTOM_THEMES + ' custom themes. Delete one first.'; saveError.style.display = 'block'; return; }
      save(slug, colorData, opts);
      applyColors(colorData);
      applyFontDensity(opts.font || DEFAULT_FONT, opts.density || DEFAULT_DENSITY);
      applyBgEffectColor(opts.bgEffectColor || '');
      applyBgPattern(opts.bgPattern || 'none');
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

function _readClankerEffectConfig() {
  const styles = getComputedStyle(document.body);
  const color = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
  const rawIntensity = parseFloat(styles.getPropertyValue('--bg-effect-intensity'));
  const effectColor = color('--bg-effect-color', color('--fg', '#62C7E8'));
  const isClanker = document.body.classList.contains('theme-clanker-dark')
    || document.body.classList.contains('theme-clanker-light');
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
    colors: isClanker ? clankerColors : clankerColors.map(() => effectColor),
  };
}

function _runBackgroundCanvas({ canvas, bodyClass, resize, paint }) {
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let animationFrame = 0;
  let animationTime = 0;
  let previousFrame = 0;
  let disposed = false;

  function dispose() {
    if (disposed) return;
    disposed = true;
    window.cancelAnimationFrame(animationFrame);
    window.removeEventListener('resize', handleResize);
    if (motion.removeEventListener) motion.removeEventListener('change', handleMotionChange);
    else if (motion.removeListener) motion.removeListener(handleMotionChange);
    document.removeEventListener('visibilitychange', handleVisibilityChange);
    if (_activeBackgroundEffectDispose === dispose) _activeBackgroundEffectDispose = null;
    canvas.remove();
  }

  function frame(time = 0) {
    if (disposed) return;
    if (!canvas.isConnected || !document.body.classList.contains(bodyClass)) {
      dispose();
      return;
    }
    if (document.hidden) {
      canvas.dataset.motion = 'paused';
      animationFrame = 0;
      return;
    }
    canvas.dataset.motion = motion.matches ? 'reduced' : 'active';
    if (!motion.matches && previousFrame) animationTime += Math.min(time - previousFrame, 50);
    previousFrame = time;
    paint(motion.matches ? 0 : animationTime, motion.matches);
    if (!motion.matches) animationFrame = window.requestAnimationFrame(frame);
  }

  function handleResize() {
    resize();
    if (motion.matches) frame(0);
  }

  function handleMotionChange() {
    window.cancelAnimationFrame(animationFrame);
    previousFrame = 0;
    frame(performance.now());
  }

  function handleVisibilityChange() {
    window.cancelAnimationFrame(animationFrame);
    previousFrame = 0;
    if (document.hidden) {
      canvas.dataset.motion = 'paused';
      animationFrame = 0;
      return;
    }
    frame(performance.now());
  }

  if (_activeBackgroundEffectDispose) _activeBackgroundEffectDispose();
  _activeBackgroundEffectDispose = dispose;
  canvas.dataset.backgroundEffectCanvas = 'true';
  canvas.__disposeEffect = dispose;
  window.addEventListener('resize', handleResize);
  if (motion.addEventListener) motion.addEventListener('change', handleMotionChange);
  else if (motion.addListener) motion.addListener(handleMotionChange);
  document.addEventListener('visibilitychange', handleVisibilityChange);
  resize();
  frame(performance.now());
}

function _mountClankerEffect({ id, bodyClass, build, draw }) {
  if (document.getElementById(id)) return;
  const canvas = document.createElement('canvas');
  canvas.id = id;
  canvas.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;';
  canvas.setAttribute('aria-hidden', 'true');
  document.body.prepend(canvas);

  const ctx = canvas.getContext('2d');
  if (!ctx) { canvas.remove(); return; }
  let width = 0;
  let height = 0;
  let dpr = 1;
  let scene = null;
  let sceneKey = '';

  function resize() {
    width = window.innerWidth;
    height = window.innerHeight;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.floor(width * dpr));
    canvas.height = Math.max(1, Math.floor(height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    sceneKey = '';
  }

  function paint(time, reduced) {
    const config = _readClankerEffectConfig();
    const nextSceneKey = `${width}:${height}:${config.size}:${config.colors.join(':')}`;
    if (sceneKey !== nextSceneKey) {
      scene = build({ width, height, ...config });
      canvas.__backgroundScene = scene;
      sceneKey = nextSceneKey;
    }
    ctx.clearRect(0, 0, width, height);
    draw(ctx, { width, height, time, reduced, scene, ...config });
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
  }

  _runBackgroundCanvas({ canvas, bodyClass, resize, paint });
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

// Stable connected route map. Tracks and nodes stay fixed while packets move.
function _initClankerRoutefield() {
  _mountClankerEffect({
    id: 'clanker-routefield-canvas',
    bodyClass: 'bg-pattern-clanker-routefield',
    build: ({ width, height, size }) => {
      const columns = Math.max(6, Math.ceil(width / (210 * size)) + 1);
      const rows = Math.max(5, Math.ceil(height / (165 * size)) + 1);
      const gapX = width / (columns - 1);
      const gapY = height / (rows - 1);
      const nodes = [];
      for (let row = 0; row < rows; row += 1) {
        for (let column = 0; column < columns; column += 1) {
          const seed = row * 97 + column * 29;
          const baseX = column * gapX;
          const baseY = row * gapY;
          nodes.push({
            x: column === 0 ? 12 : column === columns - 1 ? width - 12 : baseX + (_clankerNoise(seed) - .5) * gapX * .38,
            y: row === 0 ? 12 : row === rows - 1 ? height - 12 : baseY + (_clankerNoise(seed + 13) - .5) * gapY * .34,
            color: (row * 2 + column) % 6,
            hub: (row + column * 2) % 6 === 0,
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
        routes.push({
          a, b,
          phase: _clankerNoise(seed + 41),
          color: (a.color + b.color + seed) % 6,
          cx: (a.x + b.x) / 2 - (dy / length) * bend,
          cy: (a.y + b.y) / 2 + (dx / length) * bend,
        });
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
      return { nodes, hubs: nodes.filter(node => node.hub), routes };
    },
    draw: (ctx, { time, scene, intensity, size, colors, outline }) => {
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
      scene.routes.forEach((route, index) => {
        if (index % 4) return;
        const progress = (time / (11200 + (index % 5) * 760) + route.phase) % 1;
        const point = _pointOnQuadratic(route, progress);
        const radius = (index % 4 === 0 ? 3.4 : 2.6) * size;
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius + 1.5 * size, 0, Math.PI * 2);
        ctx.fillStyle = outline;
        ctx.globalAlpha = intensity * 0.88;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = colors[route.color];
        ctx.globalAlpha = intensity;
        ctx.fill();
      });
    },
  });
}

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
    build: ({ width, height, size }) => {
      const columns = Math.max(4, Math.ceil(width / (220 * size)));
      const rows = Math.max(4, Math.ceil(height / (220 * size)));
      const tileWidth = width / columns;
      const tileHeight = height / rows;
      const paths = [];
      const junctions = [];
      const place = (points, column, row) => points.map(([x, y]) => ({
        x: (column + x) * tileWidth,
        y: (row + y) * tileHeight,
      }));
      const placeRotated = (points, centerX, centerY) => points.map(([x, y]) => ({
        x: centerX - (y - .5) * tileWidth,
        y: centerY + (x - .5) * tileHeight,
      }));
      const addMotif = (transform, color, layer, pair) => {
        paths.push({ points: transform(upper), color, layer, pair, mirror: 0 });
        paths.push({ points: transform(lower), color: (color + 2) % 6, layer, pair, mirror: 1 });
      };

      for (let row = 0; row < rows; row += 1) {
        for (let column = 0; column < columns; column += 1) {
          const color = (row * 2 + column) % 6;
          addMotif(points => place(points, column, row), color, 0, `h:${row}:${column}`);
        }
      }

      for (let row = 0; row < rows; row += 1) {
        for (let column = 0; column <= columns; column += 1) {
          const junction = {
            x: column * tileWidth,
            y: (row + .5) * tileHeight,
            color: (row * 2 + column) % 6,
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

      const junctionNodes = junctions.map(junction => graph.get(pointKey(junction))).filter(Boolean);
      const randomWalk = seed => {
        let current = junctionNodes[Math.floor(_clankerNoise(seed) * junctionNodes.length)];
        let previous = null;
        const points = [{ x: current.x, y: current.y }];
        for (let step = 0; step < 120; step += 1) {
          let candidates = [...current.neighbors.values()].filter(node => node !== previous);
          if (!candidates.length) candidates = [...current.neighbors.values()];
          const choice = Math.floor(_clankerNoise(seed + step * 37 + current.x * .013 + current.y * .017) * candidates.length);
          const next = candidates[Math.min(choice, candidates.length - 1)];
          if (!next) break;
          points.push({ x: next.x, y: next.y });
          previous = current;
          current = next;
        }
        return points;
      };
      const snakePoints = Array.from({ length: 36 }, (_, index) => randomWalk(snakeSeed + index * 113));
      const maxSnakeStep = snakePoints.reduce((maximum, route) => Math.max(maximum,
        ...route.slice(1).map((point, index) => Math.hypot(point.x - route[index].x, point.y - route[index].y))), 0);
      return { paths, junctions, snakePoints, maxSnakeStep: maxSnakeStep, snakeSeed,
        layerVectors: [{ x: 1, y: 0 }, { x: 0, y: 1 }], mirroredPairs: paths.length / 2,
        junctionOffsetError: junctionNodes.length === junctions.length ? 0 : Infinity };
    },
    draw: (ctx, { time, scene, intensity, size, colors, outline }) => {
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      scene.paths.forEach(path => {
        ctx.beginPath();
        path.points.forEach((point, pointIndex) => pointIndex ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
        ctx.strokeStyle = outline;
        ctx.lineWidth = 3.4 * size;
        ctx.globalAlpha = intensity * (path.layer ? .2 : .24);
        ctx.stroke();
        ctx.strokeStyle = colors[path.color];
        ctx.lineWidth = 7 * size;
        ctx.globalAlpha = intensity * .025;
        ctx.stroke();
        ctx.strokeStyle = colors[path.color];
        ctx.lineWidth = 1.2 * size;
        ctx.globalAlpha = intensity * (path.layer ? .3 : .36);
        ctx.stroke();
      });
      scene.junctions.forEach(junction => {
        ctx.fillStyle = colors[junction.color];
        ctx.globalAlpha = intensity * .06;
        ctx.beginPath();
        ctx.arc(junction.x, junction.y, 7 * size, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = intensity * .7;
        ctx.beginPath();
        ctx.arc(junction.x, junction.y, 1.8 * size, 0, Math.PI * 2);
        ctx.fill();
      });

      for (let signalIndex = 0; signalIndex < 7; signalIndex += 1) {
        const duration = 16000 + signalIndex * 2300;
        const phase = time / duration + signalIndex * .31;
        const cycle = Math.floor(phase);
        const progress = phase - cycle;
        const random = scene.snakeSeed + signalIndex * 131 + cycle * 47;
        const route = scene.snakePoints[Math.floor(_clankerNoise(random) * scene.snakePoints.length)];
        const reverse = _clankerNoise(random + 17) > .5;
        const headProgress = reverse ? 1 - progress : progress;
        const fade = Math.min(1, progress * 7, (1 - progress) * 7);
        const tail = [];
        for (let step = 64; step >= 0; step -= 1) {
          const pointProgress = headProgress + (reverse ? step : -step) * .0024;
          if (pointProgress >= 0 && pointProgress <= 1) tail.push(_pointOnPolyline(route, pointProgress));
        }
        if (tail.length < 2) continue;
        const signalColor = colors[Math.floor(_clankerNoise(random + 31) * colors.length)];
        for (const [strokeStyle, lineWidth, alpha] of [
          [outline, 7.5 * size, .72],
          [signalColor, 3 * size, 1],
        ]) {
          ctx.strokeStyle = strokeStyle;
          ctx.lineWidth = lineWidth;
          ctx.shadowColor = signalColor;
          ctx.shadowBlur = strokeStyle === outline ? 0 : 18 * size;
          for (let index = 1; index < tail.length; index += 1) {
            const tailFade = Math.pow(index / (tail.length - 1), 2.25);
            ctx.beginPath();
            ctx.moveTo(tail[index - 1].x, tail[index - 1].y);
            ctx.lineTo(tail[index].x, tail[index].y);
            ctx.globalAlpha = intensity * alpha * fade * tailFade;
            ctx.stroke();
          }
        }
        ctx.shadowBlur = 0;
        const head = tail[tail.length - 1];
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

function _initClankerRadar() {
  _mountClankerEffect({
    id: 'clanker-radar-canvas',
    bodyClass: 'bg-pattern-clanker-radar',
    build: ({ width, height }) => {
      const base = Math.min(width, height);
      return { centers: [
        { x: width * -.01, y: height * .2, radius: base * .18, color: 4, phase: .08 },
        { x: width * .25, y: height * .3, radius: base * .17, color: 2, phase: .31 },
        { x: width * .55, y: height * .18, radius: base * .15, color: 0, phase: .52 },
        { x: width * .84, y: height * .34, radius: base * .21, color: 3, phase: .74 },
        { x: width * .16, y: height * .73, radius: base * .19, color: 1, phase: .93 },
        { x: width * .52, y: height * .72, radius: base * .18, color: 5, phase: .43 },
        { x: width * .88, y: height * .83, radius: base * .17, color: 4, phase: .19 },
      ] };
    },
    draw: (ctx, { time, scene, intensity, size, colors, outline }) => {
      scene.centers.forEach((center, centerIndex) => {
        ctx.beginPath();
        ctx.arc(center.x, center.y, center.radius, 0, Math.PI * 2);
        ctx.fillStyle = colors[center.color];
        ctx.globalAlpha = intensity * .025;
        ctx.fill();

        for (let ring = 1; ring <= 5; ring += 1) {
          const radius = center.radius * ring / 5;
          ctx.beginPath();
          ctx.arc(center.x, center.y, radius, 0, Math.PI * 2);
          ctx.strokeStyle = ring === 5 ? outline : colors[(center.color + ring) % colors.length];
          ctx.lineWidth = (ring === 5 ? 2.8 : 1.1) * size;
          ctx.globalAlpha = intensity * (ring === 5 ? .5 : .22);
          ctx.stroke();

          const direction = ring % 2 ? 1 : -.55;
          const arcStart = center.phase * Math.PI * 2 + ring * 1.37 + time / (11500 + centerIndex * 740) * direction;
          ctx.beginPath();
          ctx.arc(center.x, center.y, radius, arcStart, arcStart + .34 + ring * .12);
          ctx.strokeStyle = colors[(center.color + ring + 1) % colors.length];
          ctx.lineWidth = (ring % 3 === 0 ? 4 : 2.2) * size;
          ctx.globalAlpha = intensity * .7;
          ctx.stroke();
        }

        for (let tick = 0; tick < 12; tick += 1) {
          const angle = tick / 12 * Math.PI * 2 + center.phase;
          const inner = center.radius * (tick % 3 === 0 ? .89 : .94);
          ctx.beginPath();
          ctx.moveTo(center.x + Math.cos(angle) * inner, center.y + Math.sin(angle) * inner);
          ctx.lineTo(center.x + Math.cos(angle) * center.radius, center.y + Math.sin(angle) * center.radius);
          ctx.strokeStyle = colors[(center.color + tick) % colors.length];
          ctx.lineWidth = (tick % 3 === 0 ? 2 : 1) * size;
          ctx.globalAlpha = intensity * .55;
          ctx.stroke();
        }

        const angle = center.phase * Math.PI * 2 + time / (13800 + centerIndex * 920);
        ctx.beginPath();
        ctx.moveTo(center.x, center.y);
        ctx.arc(center.x, center.y, center.radius * .96, angle - .28, angle);
        ctx.closePath();
        ctx.fillStyle = colors[center.color];
        ctx.globalAlpha = intensity * .055;
        ctx.fill();
        ctx.beginPath();
        ctx.moveTo(center.x, center.y);
        ctx.lineTo(center.x + Math.cos(angle) * center.radius * .96, center.y + Math.sin(angle) * center.radius * .96);
        ctx.strokeStyle = colors[center.color];
        ctx.lineWidth = 2.4 * size;
        ctx.globalAlpha = intensity * .58;
        ctx.stroke();

        for (let blip = 0; blip < 3; blip += 1) {
          const blipAngle = center.phase * 9 + blip * 2.31;
          const blipRadius = center.radius * (.28 + blip * .24);
          const pulse = 1 + Math.sin(time / 1250 + blipAngle) * .24;
          const x = center.x + Math.cos(blipAngle) * blipRadius;
          const y = center.y + Math.sin(blipAngle) * blipRadius;
          ctx.beginPath();
          ctx.arc(x, y, (2.4 + blip) * pulse * size, 0, Math.PI * 2);
          ctx.fillStyle = colors[(center.color + blip + 2) % colors.length];
          ctx.globalAlpha = intensity * .82;
          ctx.fill();
        }

        ctx.beginPath();
        ctx.arc(center.x, center.y, 4.4 * size, 0, Math.PI * 2);
        ctx.fillStyle = colors[center.color];
        ctx.globalAlpha = intensity;
        ctx.fill();
      });
    },
  });
}

function _clankerNoise(seed) {
  const value = Math.sin(seed * 12.9898 + 78.233) * 43758.5453;
  return value - Math.floor(value);
}

function _initClankerGemDrift() {
  _mountClankerEffect({
    id: 'clanker-gem-drift-canvas',
    bodyClass: 'bg-pattern-clanker-gem-drift',
    build: ({ width, height, size }) => {
      const count = Math.max(140, Math.ceil(width * height / 5600));
      return { shards: Array.from({ length: count }, (_, index) => {
        const seed = index * 47 + 11;
        return {
          x: _clankerNoise(seed) * width,
          y: _clankerNoise(seed + 5) * height,
          radius: (4 + _clankerNoise(seed + 9) * 10) * size,
          stretch: .7 + _clankerNoise(seed + 13) * .55,
          rotation: _clankerNoise(seed + 17) * Math.PI * 2,
          color: index % 6,
          shape: index % 4,
          phase: _clankerNoise(seed + 23) * Math.PI * 2,
          drift: (3 + _clankerNoise(seed + 29) * 9) * size,
          bright: index % 9 === 0,
        };
      }) };
    },
    draw: (ctx, { time, scene, intensity, size, colors, outline }) => {
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      scene.shards.forEach((shard, index) => {
        const driftX = Math.sin(time / (5900 + index % 7 * 340) + shard.phase) * shard.drift;
        const driftY = Math.cos(time / (7000 + index % 5 * 410) + shard.phase) * shard.drift * .72;
        const radius = shard.radius;
        ctx.save();
        ctx.translate(shard.x + driftX, shard.y + driftY);
        ctx.rotate(shard.rotation + Math.sin(time / 8400 + shard.phase) * .12);
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
        ctx.lineWidth = (shard.bright ? 2 : 1.4) * size;
        ctx.globalAlpha = intensity * (shard.bright ? .62 : .22);
        if (shard.bright) { ctx.shadowColor = colors[shard.color]; ctx.shadowBlur = 9 * size; }
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.globalAlpha = intensity * (shard.bright ? .8 : .34);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(0, -radius * .7); ctx.lineTo(0, 0); ctx.lineTo(radius * .5, radius * .34);
        ctx.strokeStyle = colors[(shard.color + 2) % colors.length];
        ctx.lineWidth = .9 * size;
        ctx.globalAlpha = intensity * (shard.bright ? .58 : .26);
        ctx.stroke();
        ctx.restore();
      });
    },
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
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const GRID = 92;
  const MAX_PULSES = 18;
  const TRAIL_LEN = 42;

  let W, H, cols, rows, pulses = [], neurons = [], edges = [];

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
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
    const { colors, intensity, size } = _readClankerEffectConfig();
    edges.forEach(edge => {
      ctx.beginPath(); ctx.moveTo(edge.a.x, edge.a.y); ctx.lineTo(edge.b.x, edge.b.y);
      ctx.strokeStyle = colors[edge.color]; ctx.lineWidth = .7 * size; ctx.globalAlpha = intensity * .12; ctx.stroke();
    });
    neurons.forEach(node => {
      const pulse = .72 + Math.sin(time / 2600 + node.phase) * .18;
      ctx.beginPath(); ctx.arc(node.x, node.y, node.radius * size * pulse, 0, Math.PI * 2);
      ctx.fillStyle = colors[node.color]; ctx.globalAlpha = intensity * .34; ctx.fill();
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
      ctx.globalAlpha = intensity * .56;
      ctx.lineWidth = 1.4 * size;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.globalAlpha = intensity * .9;
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
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H;
  let drops = [];
  const MAX_DROPS = 130;

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
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
    const { colors, intensity, size } = _readClankerEffectConfig();
    drops.forEach(d => {
      const effLen = d.len * size;
      const span = H + effLen * 2;
      const y = ((d.phase + time / 1000 * d.speed) % 1) * span - effLen;
      const grad = ctx.createLinearGradient(d.x, y - effLen, d.x, y);
      grad.addColorStop(0, 'transparent');
      grad.addColorStop(1, colors[d.color]);
      ctx.strokeStyle = grad;
      ctx.globalAlpha = intensity * d.alpha;
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
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H;
  const STAR_COUNT = 88;
  const CONNECT_DIST = 148;
  let stars = [];

  function resize() {
    W = window.innerWidth; H = window.innerHeight;
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
    const { colors, intensity, size } = _readClankerEffectConfig();
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
          ctx.globalAlpha = intensity * (1 - dist / CONNECT_DIST) * .16;
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
      ctx.globalAlpha = intensity * (.18 + twinkle * .34);
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
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  let W, H, streams = [];
  function resize() {
    W = window.innerWidth; H = window.innerHeight;
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
    const { colors, intensity, size } = _readClankerEffectConfig();
    streams.forEach((stream, index) => {
      ctx.beginPath();
      stream.points.forEach((point, pointIndex) => pointIndex ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
      ctx.strokeStyle = colors[stream.color];
      ctx.lineWidth = Math.max(.65, size * .85);
      ctx.globalAlpha = intensity * .16;
      ctx.stroke();
      if (index % 6 !== 0) return;
      const head = _pointOnPolyline(stream.points, time / (10500 + index * 37) + stream.phase);
      ctx.beginPath(); ctx.arc(head.x, head.y, 2.1 * size, 0, Math.PI * 2);
      ctx.fillStyle = colors[(stream.color + 2) % colors.length];
      ctx.globalAlpha = intensity * .8; ctx.fill();
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
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
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
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    petals = Array.from({ length:64 }, (_, index) => makePetal(index));
  }
  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, intensity, size:sz } = _readClankerEffectConfig();
    petals.forEach(p => {
      const progress = (p.phase + time / 1000 * p.speed) % 1;
      const y = progress * (H + 40) - 20;
      const x = p.x + Math.sin(time / 2600 + p.drift) * p.wobble;
      ctx.save(); ctx.translate(x, y); ctx.rotate(p.rot + time * p.vr);
      ctx.globalAlpha = intensity * .24;
      ctx.fillStyle = colors[p.color];
      ctx.beginPath(); ctx.ellipse(-p.size * 0.2 * sz, 0, p.size * 0.6 * sz, p.size * 0.3 * sz, 0.3, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = intensity * .16;
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
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
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
    const { colors, intensity, size:sizeMult } = _readClankerEffectConfig();
    sparkles.forEach(s => {
      const glow = .5 + .5 * Math.sin(time * s.speed + s.phase);
      const alpha = intensity * (.06 + glow * (s.bright ? .42 : .2)) * s.life;
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
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
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
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    embers = Array.from({ length:96 }, (_, index) => makeEmber(index));
  }
  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    const { colors, intensity, size:sz } = _readClankerEffectConfig();
    ctx.globalCompositeOperation = 'lighter';
    embers.forEach(e => {
      const lifeRatio = (e.phase + time / 1000 * e.speed) % 1;
      const fade = Math.min(1, lifeRatio * 7, (1 - lifeRatio) * 7);
      const x = e.x + Math.sin(time / 1800 + e.wobble) * e.drift;
      const y = H + 18 - lifeRatio * (H + 36);
      const r = e.r * sz;
      ctx.fillStyle = colors[e.color];
      ctx.globalAlpha = intensity * fade * (e.bright ? .82 : .38);
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
                       getCustomThemes };

export default themeModule;

// Init on DOM ready, with server-side sync fallback
async function _initWithSync() {
  // If no local theme, try loading from server (cross-device sync)
  if (!getSaved()) {
    const serverTheme = await _loadFromServer();
    if (serverTheme && serverTheme.colors) {
      if (serverTheme.name === 'sakura') serverTheme.name = 'ume';
      Storage.setJSON(LS_KEY, serverTheme);
      applyColors(serverTheme.colors);
    }
  }
  // Also sync custom themes from server
  try {
    const res = await fetch('/api/prefs/custom-themes', { credentials: 'same-origin' });
    const data = await res.json();
    if (data.value && typeof data.value === 'object') {
      const local = _loadCustomThemes();
      // Merge: server themes fill in missing local ones
      let changed = false;
      for (const [name, colors] of Object.entries(data.value)) {
        if (!local[name]) { local[name] = colors; changed = true; }
      }
      if (changed) _saveCustomThemes(local);
    }
  } catch (e) { console.warn('Custom theme server sync failed:', e); }
  initThemeUI();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => _initWithSync());
} else {
  _initWithSync();
}
