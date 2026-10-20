#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const I18N_DIR = path.join(ROOT, 'static', 'i18n');
const require = createRequire(import.meta.url);
// Babel is only required for JavaScript AST extraction. Validation, freeze and
// structured extraction must run without installing Copal's node_modules.
let babelParser = null;
let traverse = null;
function loadBabel() {
  if (babelParser && traverse) return;
  babelParser = require(path.join(ROOT, 'packages', 'Copal', 'node_modules', '@babel', 'parser'));
  const traverseModule = require(path.join(ROOT, 'packages', 'Copal', 'node_modules', '@babel', 'traverse'));
  traverse = traverseModule.default || traverseModule;
}

const LOCALE_SEEDS = {
  en: { name: 'English', target: 'English', dir: 'ltr' },
  'zh-Hans': { name: '简体中文', target: 'Simplified Chinese', dir: 'ltr' },
  ja: { name: '日本語', target: 'Japanese', dir: 'ltr' },
  ko: { name: '한국어', target: 'Korean', dir: 'ltr' },
  es: { name: 'Español', target: 'neutral international Spanish', dir: 'ltr' },
  hi: { name: 'हिन्दी', target: 'Hindi', dir: 'ltr' },
  ar: { name: 'العربية', target: 'Modern Standard Arabic', dir: 'rtl' },
  ru: { name: 'Русский', target: 'Russian', dir: 'ltr' },
  pt: { name: 'Português', target: 'broadly understood neutral Portuguese', dir: 'ltr' },
  id: { name: 'Bahasa Indonesia', target: 'standard Indonesian', dir: 'ltr' },
  'pa-Guru': { name: 'ਪੰਜਾਬੀ', target: 'Punjabi in Gurmukhi script', dir: 'ltr' },
  bn: { name: 'বাংলা', target: 'standard Bengali', dir: 'ltr' },
  sw: { name: 'Kiswahili', target: 'standard Swahili', dir: 'ltr' },
  ur: { name: 'اردو', target: 'standard Urdu', dir: 'rtl' },
  fa: { name: 'فارسی', target: 'contemporary Persian', dir: 'rtl' },
};

// Keep the audited canonical registry authoritative when extraction runs.
// Long-tail locales can point at the English fallback catalog until their
// translated files arrive; extraction must not silently shrink the registry
// back to the original fifteen-language list.
const existingRegistry = (() => {
  try {
    const value = JSON.parse(fs.readFileSync(path.join(I18N_DIR, 'registry.json'), 'utf8'));
    return value && typeof value === 'object' ? value : {};
  } catch (_) { return {}; }
})();
const pickLocaleMeta = (meta = {}) => ({
  name: meta.name || meta.id || undefined,
  target: meta.target || meta.name || undefined,
  dir: meta.dir || 'ltr',
  ...(meta.catalog ? { catalog: meta.catalog } : {}),
  ...(meta.html_lang ? { html_lang: meta.html_lang } : {}),
  ...(meta.alias_of ? { alias_of: meta.alias_of } : {}),
  ...(meta.display_only ? { display_only: true } : {}),
});
const LOCALES = Object.freeze({
  ...Object.fromEntries(Object.entries(LOCALE_SEEDS).map(([id, item]) => [id, {
    ...item,
    ...(existingRegistry.locales?.[id]?.catalog ? { catalog: existingRegistry.locales[id].catalog } : {}),
    ...(existingRegistry.locales?.[id]?.html_lang ? { html_lang: existingRegistry.locales[id].html_lang } : {}),
    ...(existingRegistry.locales?.[id]?.alias_of ? { alias_of: existingRegistry.locales[id].alias_of } : {}),
    ...(existingRegistry.locales?.[id]?.display_only ? { display_only: true } : {}),
  }])),
  ...Object.fromEntries(Object.entries(existingRegistry.locales || {})
    .filter(([id]) => !LOCALE_SEEDS[id])
    .map(([id, meta]) => [id, { id, ...pickLocaleMeta(meta) }])),
});

const BRANDS = Object.freeze([
  'Open Clank', 'OpenClank', 'Copal', 'Clanker', 'MiMo', 'OpenAI', 'ChatGPT', 'Codex',
  'Anthropic', 'Claude', 'Google', 'Gemini', 'GitHub', 'Gmail', 'Microsoft',
  'Outlook', 'Matrix', 'Discord', 'Slack', 'Notion', 'Box', 'Figma',
  'Atlassian', 'Rovo', 'SharePoint', 'Teams', 'Obsidian', 'Godot',
  'Hugging Face', 'Ollama', 'llama.cpp', 'TreeHouse', 'Frankenmemory',
  'FastAPI', 'Next.js', 'React', 'Radix', 'CodeMirror',
]);

const STABLE_TOKENS = Object.freeze([
  'API', 'JSON', 'HTML', 'CSS', 'JavaScript', 'TypeScript', 'Python', 'Rust',
  'OAuth', 'MCP', 'URL', 'HTTP', 'HTTPS', 'PDF', 'PWA', 'TOTP', 'CalDAV',
  'IMAP', 'SMTP', 'WebSocket', 'SSE', 'SQL', 'Markdown', 'CSV', 'ZIP',
]);

const JS_EXCLUDES = [
  /\/static\/lib\//,
  /\.min\.js$/,
  /\/static\/js\/i18n\.js$/,
  // The source editor build emits both codemirror.js and content-hashed
  // siblings. They are generated/vendor code and must never become UI keys.
  /\/static\/js\/copal\/codemirror(?:[-.].*)?\.js$/,
  /\/static\/js\/(?:[^/]+[-.]?)?chunk(?:[-.].*)?\.js$/,
  /\/static\/js\/(?:[^/]+[-.]?)?vendor(?:[-.].*)?\.js$/,
  /\/static\/js\/copal\/[^/]+\.chunk\.[^/]+\.js$/,
  /\/static\/js\/modelCatalog\.js$/,
  /\/static\/js\/mimoModels\.js$/,
  /\/static\/js\/mimoProviders\.generated\.js$/,
  /\/node_modules\//,
  /\/\.references\//,
  /\/\.clanker\/references\//,
  /\/packages\/mimo-code\//,
  /\/packages\/Copal\/src\/components\/ui\//,
  /\/packages\/Copal\/src\/lib\/emoji-data\.ts$/,
];

const HTML_FILES = [
  'static/index.html',
  'static/login.html',
  'static/treehouse-architecture-map.html',
  'static/treehouse-course-overview.html',
  'static/treehouse-troubleshooting.html',
  'packages/Copal/ui/index.html',
];

const PYTHON_ROOTS = ['app.py', 'routes', 'companion', 'src', 'services'];
const JSON_UI_FILES = ['static/manifest.json'];
const SOURCE_EXTENSIONS = new Set(['.js', '.mjs', '.ts', '.tsx', '.jsx']);
const BIDI_CONTROLS = /[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/u;
const PLACEHOLDER = /\{([a-zA-Z_][a-zA-Z0-9_]*|\d+)\}/g;
const CSS_PROPERTY = /^(?:--|align|animation|appearance|backdrop|background|border|bottom|box|color|column|content|cursor|display|fill|filter|flex|float|font|gap|grid|height|inset|justify|left|letter|line|margin|max|min|object|opacity|order|outline|overflow|padding|perspective|pointer|position|right|table|text|top|transform|transition|user|vertical|visibility|white|width|word|z-index|zoom)(?:-[a-z]+)*$/iu;
const HTML_SELECTOR_TAG = /^(?:a|article|button|code|div|form|h[1-6]|input|label|li|main|option|p|pre|select|span|table|td|textarea|th|tr|ul)$/iu;

function walk(target) {
  const rel = path.relative(ROOT, target).split(path.sep).join('/');
  if (/(?:^|\/)(?:\.references|\.clanker\/references)(?:\/|$)/.test(rel)) return [];
  if (!fs.existsSync(target)) return [];
  const stat = fs.statSync(target);
  if (stat.isFile()) return [target];
  return fs.readdirSync(target, { withFileTypes: true })
    .flatMap(entry => walk(path.join(target, entry.name)));
}

function relative(file) {
  return path.relative(ROOT, file).split(path.sep).join('/');
}

function normalizeSource(raw) {
  return String(raw ?? '')
    .replace(/\r\n?/g, '\n')
    .replace(/[\t\n ]+/g, ' ')
    .trim();
}

function looksUserFacing(raw) {
  const value = normalizeSource(raw);
  if (value.length < 2 || value.length > 600 || !/[A-Za-z]/.test(value)) return false;
  // These values are implementation literals collected from querySelector,
  // style assignments, MIME allowlists and embedded templates. Keep their
  // historical keys for compatibility, but never treat them as live UI copy.
  if (/^\{\{[\s\S]*\}\}$/u.test(value)) return false;
  if (HTML_SELECTOR_TAG.test(value) && (value === value.toLowerCase() || value === value.toUpperCase())) return false;
  if (/^(?:[-\w]+\.)?[a-z0-9_-]+\.(?:json|memes|zip),application\/[a-z0-9.+-]+(?:,|$)/iu.test(value)) return false;
  if (/^(?:[.#][\w-]+|\[[^\]]+\]|(?:select|div|span|button|input|textarea|form|option|table|tr|td|th|h[1-6])(?:[.#:[\s]|$))/u.test(value)) return false;
  if (/^(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD|SELECT|INSERT|UPDATE|DROP)\b(?:\s|$)/u.test(value)) return false;
  if (/^(?:python3|python|node|bash|sh)\s+-c\b|^\[\s*["']-[a-z]/iu.test(value)) return false;
  if (/^(?:[a-z][\w-]*:){1,2}[\w.-]+$/u.test(value)) return false;
  const declarations = value.split(';').map(part => part.trim()).filter(Boolean);
  if (declarations.some(part => {
    const match = part.match(/^([\w-]+)\s*:\s*(.+)$/u);
    if (!match || !CSS_PROPERTY.test(match[1])) return false;
    return declarations.length > 1 || /(?:[-\d.]|var\(|#|\{|\b(?:auto|none|inherit|initial|transparent|flex|block|inline|absolute|relative|fixed|hidden|pointer)\b)/iu.test(match[2]);
  })) return false;
  // Selectors, CSS declarations and generated module identifiers are source
  // implementation details even when they occur in a UI-adjacent object.
  if (/^(?:[.#][\w-]+|--[\w-]+)(?:[\s>+~:#.\[\]=*"'-].*)?$/u.test(value)) return false;
  if (/^(?:[a-z-]+)\s*:\s*(?:[a-z0-9#.%(), -]+)$/iu.test(value)) return false;
  if (/^[\w-]+\.(?:css|js|mjs|ts|tsx|jsx|json|html)(?:[?#].*)?$/iu.test(value)) return false;
  if (/^(?:GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s+\//u.test(value)) return false;
  if (/<\/?[a-z][^>]*>/i.test(value)) return false;
  if (/^\{[^{}]+\}$/.test(value) || /\{[^{}]*(?:[().]|::)[^{}]*\}/.test(value)) return false;
  if (/^(?:&#(?:x[0-9a-f]+|\d+);|\\[0-9a-f]{2,6})$/i.test(value)) return false;
  if (/^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$/.test(value)) return false;
  if (/^[A-Za-z]+Error$/.test(value)) return false;
  if (/^\d+(?:\.\d+)?x$/i.test(value)) return false;
  if (/^(?:-?\d+(?:\.\d+)?(?:px|rem|em|vh|vw|%|ms|s)?(?:\s|$)|rgba?\(|hsla?\(|color-mix\(|var\(--|calc\()/i.test(value)) return false;
  if (/\b(?:rgba?|hsla?|color-mix|linear-gradient|radial-gradient|box-shadow|translate[XY]?|scale[XY]?|rotate)\s*\(/i.test(value)) return false;
  if (/^(?:[a-z0-9:[\]()./%-]+\s+){3,}[a-z0-9:[\]()./%-]+$/i.test(value) && /[-:[\]]/.test(value)) return false;
  if (/^(?:https?:|data:|blob:|mailto:|tel:|\/api\/|\/static\/|\.\/|\.\.\/)/i.test(value)) return false;
  if (/^(?:#[\w-]+|\.[\w-]+|--[\w-]+|[\w-]+\.(?:js|css|py|rs|ts|tsx|json|md|html|svg|png|jpe?g|gif|webp|woff2?))$/i.test(value)) return false;
  if (/^[a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9_-]+)+$/.test(value)) return false;
  if (/^[a-z][a-zA-Z0-9_]*$/.test(value) && /[A-Z_]/.test(value)) return false;
  if (/^[\w-]+\/[\w./-]+$/.test(value)) return false;
  if (/^[\w.-]+@[\w.-]+$/.test(value)) return false;
  if (/^[{}[\]().,:;!?+*=|&%$#@~`"'\\\/-]+$/.test(value)) return false;
  const symbols = (value.match(/[{}[\]<>_=\\/]/g) || []).length;
  if (symbols > Math.max(6, value.length / 5)) return false;
  if (/^(?:GET|POST|PUT|PATCH|DELETE) \/\S+/.test(value)) return false;
  return true;
}

function slugFor(source) {
  const withoutPlaceholders = source.replace(PLACEHOLDER, ' value ');
  const slug = withoutPlaceholders
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '.')
    .replace(/^\.+|\.+$/g, '')
    .split('.')
    .filter(Boolean)
    .slice(0, 10)
    .join('.')
    .slice(0, 72);
  return `ui.${slug || 'message'}`;
}

function hashText(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function makeCollector(existingEnglish = {}) {
  const bySource = new Map(Object.entries(existingEnglish).map(([key, source]) => [source, key]));
  const byKey = new Map(Object.entries(existingEnglish));
  const entries = new Map();

  function keyFor(source) {
    if (bySource.has(source)) return bySource.get(source);
    const base = slugFor(source);
    if (!byKey.has(base) || byKey.get(base) === source) {
      byKey.set(base, source);
      bySource.set(source, base);
      return base;
    }
    const suffix = crypto.createHash('sha1').update(source).digest('hex').slice(0, 8);
    const key = `${base}.${suffix}`;
    byKey.set(key, source);
    bySource.set(source, key);
    return key;
  }

  function add(raw, file, line, kind = 'literal') {
    const source = normalizeSource(raw);
    if (!looksUserFacing(source)) return;
    const key = keyFor(source);
    const record = entries.get(key) || { key, source, kind, locations: [] };
    const location = `${relative(file)}:${line || 1}`;
    if (!record.locations.includes(location)) record.locations.push(location);
    if (record.kind !== kind) record.kind = 'mixed';
    entries.set(key, record);
  }

  return { add, entries };
}

function extractHtmlText(raw, file, baseLine, collector, kind = 'html') {
  const withoutComments = raw
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<script\b[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[\s\S]*?<\/style>/gi, ' ')
    .replace(/<(?:code|pre)\b[\s\S]*?<\/(?:code|pre)>/gi, ' ');

  const attrPattern = /\b(?:placeholder|title|aria-label|aria-description|alt)\s*=\s*(["'])([\s\S]*?)\1/gi;
  let match;
  while ((match = attrPattern.exec(withoutComments))) {
    const line = baseLine + withoutComments.slice(0, match.index).split('\n').length - 1;
    collector.add(match[2], file, line, `${kind}-attribute`);
  }

  const textPattern = />([^<>]+)</g;
  while ((match = textPattern.exec(withoutComments))) {
    const text = match[1]
      .replace(/&nbsp;/gi, ' ')
      .replace(/&amp;/gi, '&')
      .replace(/&lt;/gi, '<')
      .replace(/&gt;/gi, '>')
      .replace(/&quot;/gi, '"')
      .replace(/&#39;/gi, "'");
    const line = baseLine + withoutComments.slice(0, match.index).split('\n').length - 1;
    collector.add(text, file, line, `${kind}-text`);
  }
}

function templateSource(node) {
  let value = '';
  node.quasis.forEach((quasi, index) => {
    value += quasi.value.cooked ?? quasi.value.raw;
    if (index < node.expressions.length) value += `{${index}}`;
  });
  return value;
}

function skipStringPath(astPath) {
  const parent = astPath.parent;
  if (!parent) return false;
  if (parent.type === 'ImportDeclaration' || parent.type === 'ExportNamedDeclaration' || parent.type === 'ExportAllDeclaration') return true;
  if ((parent.type === 'ObjectProperty' || parent.type === 'ObjectMethod') && parent.key === astPath.node && !parent.computed) return true;
  if ((parent.type === 'MemberExpression' || parent.type === 'OptionalMemberExpression') && parent.property === astPath.node && !parent.computed) return true;
  if (parent.type === 'Directive' || parent.type === 'DirectiveLiteral') return true;
  if (parent.type === 'CallExpression' && parent.callee?.type === 'Import') return true;
  return false;
}

const UI_PROPERTIES = new Set([
  'text', 'textContent', 'innerText', 'innerHTML', 'label', 'title', 'tooltip',
  'placeholder', 'description', 'message', 'help', 'hint', 'caption', 'heading',
  'aria-label', 'ariaLabel', 'aria-description', 'ariaDescription', 'alt',
  'emptyText', 'errorText', 'loadingText', 'confirmText', 'cancelText',
]);

const UI_CALLS = /(?:^|\.)(?:h|createElement|setAttribute|showToast|toast|notify|alert|confirm|prompt|showError|showMessage|setStatus|setMessage|setText|openConfirm|openPrompt|styledConfirm|styledPrompt|renderMenu|showChooser|showCommands|addOption|addItem)$/i;
const UI_CONTAINER_NAMES = /^(?:.*(?:label|title|text|message|description|tooltip|placeholder|help|hint|caption|heading|tabs?|options?|actions?|commands?|menus?|statuses|errors?|empty|loading|confirm|cancel).*)$/i;

function memberName(node) {
  if (!node) return '';
  if (node.type === 'Identifier') return node.name;
  if (node.type === 'StringLiteral') return node.value;
  if (node.type === 'MemberExpression' || node.type === 'OptionalMemberExpression') {
    return `${memberName(node.object)}.${memberName(node.property)}`;
  }
  return '';
}

function isUiContext(astPath) {
  let current = astPath;
  for (let depth = 0; current?.parentPath && depth < 6; depth += 1, current = current.parentPath) {
    const parent = current.parentPath.node;
    if ((parent.type === 'ObjectProperty' || parent.type === 'ObjectMethod') && parent.value === current.node) {
      const key = memberName(parent.key);
      if (UI_PROPERTIES.has(key) || UI_CONTAINER_NAMES.test(key)) return true;
    }
    if (parent.type === 'JSXAttribute') {
      const name = memberName(parent.name);
      if (UI_PROPERTIES.has(name)) return true;
    }
    if (parent.type === 'AssignmentExpression' && parent.right === current.node) {
      const name = memberName(parent.left);
      const leaf = name.split('.').at(-1);
      if (UI_PROPERTIES.has(leaf) || UI_CONTAINER_NAMES.test(leaf)) return true;
    }
    if (parent.type === 'CallExpression') {
      const name = memberName(parent.callee);
      if (UI_CALLS.test(name) || UI_CONTAINER_NAMES.test(name.split('.').at(-1))) return true;
    }
    if (parent.type === 'VariableDeclarator') {
      const name = memberName(parent.id);
      if (UI_CONTAINER_NAMES.test(name)) return true;
    }
  }
  return false;
}

function isLikelyStandaloneMessage(value) {
  if (value.length < 4) return false;
  if (/^(?:Loading|Saving|Saved|Failed|Unable|Error|Warning|Delete|Remove|Add|Create|Edit|Open|Close|Cancel|Confirm|Search|Select|Choose|No |Show|Hide|Enable|Disable|Copy|Copied|Download|Upload|Export|Import|Refresh|Retry|Start|Stop|Run|Running|Ready|Connected|Disconnected|Unknown)\b/i.test(value)) return true;
  return false;
}

function extractJavaScript(file, collector) {
  loadBabel();
  const code = fs.readFileSync(file, 'utf8');
  let ast;
  try {
    ast = babelParser.parse(code, {
      sourceType: 'unambiguous',
      allowAwaitOutsideFunction: true,
      allowReturnOutsideFunction: true,
      errorRecovery: true,
      plugins: [
        'jsx', 'typescript', 'decorators-legacy', 'classProperties',
        'classPrivateProperties', 'classPrivateMethods', 'dynamicImport',
        'importMeta', 'topLevelAwait',
      ],
    });
  } catch (error) {
    process.stderr.write(`parse warning: ${relative(file)}: ${error.message}\n`);
    return;
  }

  traverse(ast, {
    StringLiteral(astPath) {
      if (skipStringPath(astPath)) return;
      const value = astPath.node.value;
      const line = astPath.node.loc?.start.line || 1;
      if (/<[a-z][\s\S]*>/i.test(value)) {
        extractHtmlText(value, file, line, collector, 'js-string-html');
      } else if (isUiContext(astPath) || isLikelyStandaloneMessage(normalizeSource(value))) {
        collector.add(value, file, line, 'js-string');
      }
    },
    TemplateLiteral(astPath) {
      if (astPath.parent?.type === 'TaggedTemplateExpression') return;
      const source = templateSource(astPath.node);
      const line = astPath.node.loc?.start.line || 1;
      if (/<[a-z][\s\S]*>/i.test(source)) extractHtmlText(source, file, line, collector, 'js-template-html');
      else if (isUiContext(astPath) || isLikelyStandaloneMessage(normalizeSource(source))) collector.add(source, file, line, 'js-template');
    },
    JSXText(astPath) {
      collector.add(astPath.node.value, file, astPath.node.loc?.start.line, 'jsx-text');
    },
  });
}

function extractQuotedSource(file, collector) {
  const code = fs.readFileSync(file, 'utf8');
  const triplePattern = /"""([\s\S]*?)"""|'''([\s\S]*?)'''/g;
  let match;
  const htmlRanges = [];
  while ((match = triplePattern.exec(code))) {
    const raw = match[1] ?? match[2] ?? '';
    if (/<(?:html|body|main|div|form|h1|h2|p|button|label|input)\b/i.test(raw)) {
      extractHtmlText(raw, file, code.slice(0, match.index).split('\n').length, collector, 'server-html');
    }
    htmlRanges.push([match.index, triplePattern.lastIndex]);
  }
  const lines = code.split('\n');
  const context = /(?:HTTPException|JSONResponse|HTMLResponse|detail\s*=|["'](?:error|message|detail|status|title|description|label)["']\s*:|raise\s+(?:ValueError|RuntimeError)|return\s+\{)/;
  const quoted = /(["'])((?:\\.|(?!\1).){2,600})\1/g;
  let offset = 0;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const inTriple = htmlRanges.some(([start, end]) => offset >= start && offset < end);
    offset += line.length + 1;
    if (inTriple) continue;
    if (/<[a-z][^>]*>/i.test(line)) {
      while ((match = quoted.exec(line))) {
        const decoded = match[2].replace(/\\n/g, ' ').replace(/\\t/g, ' ').replace(/\\(["'])/g, '$1');
        if (/<[a-z][\s\S]*>/i.test(decoded)) extractHtmlText(decoded, file, index + 1, collector, 'server-html');
      }
      quoted.lastIndex = 0;
    }
    if (!context.test(line)) continue;
    while ((match = quoted.exec(line))) {
      const decoded = match[2].replace(/\\n/g, ' ').replace(/\\t/g, ' ').replace(/\\(["'])/g, '$1');
      if (isLikelyStandaloneMessage(normalizeSource(decoded))) collector.add(decoded, file, index + 1, 'server-string');
    }
    quoted.lastIndex = 0;
  }
}

function extractCss(file, collector) {
  const code = fs.readFileSync(file, 'utf8');
  const pattern = /\bcontent\s*:\s*(["'])(.*?)\1/g;
  let match;
  while ((match = pattern.exec(code))) {
    collector.add(match[2], file, code.slice(0, match.index).split('\n').length, 'css-content');
  }
}

function readJson(file, fallback = {}) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}

function catalogRecords(payload) {
  // Donor revisions exist in both the historical flat object shape and the
  // source/context/messages envelope. Keep the importer schema agnostic while
  // leaving this build's flat runtime catalogs untouched.
  if (payload && payload.schema === 1 && Array.isArray(payload.messages)) {
    return payload.messages
      .filter(record => record && typeof record.source === 'string')
      .map(record => ({
        key: typeof record.key === 'string' ? record.key : null,
        source: record.source,
        context: typeof record.context === 'string' ? record.context : '',
        target: typeof record.target === 'string' ? record.target : undefined,
      }));
  }
  if (!payload || Array.isArray(payload) || typeof payload !== 'object') return [];
  return Object.entries(payload).map(([key, target]) => ({
    key,
    source: key,
    context: '',
    target: typeof target === 'string' ? target : undefined,
  }));
}

function gitFile(donor, revision, file) {
  try {
    return execFileSync('git', ['-C', donor, 'show', `${revision}:${file}`], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
  } catch (_) {
    return null;
  }
}

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
}

function sourceFiles() {
  const staticJs = walk(path.join(ROOT, 'static'))
    .filter(file => SOURCE_EXTENSIONS.has(path.extname(file)))
    .filter(file => !JS_EXCLUDES.some(pattern => pattern.test(file)));
  const copal = walk(path.join(ROOT, 'packages', 'Copal', 'src'))
    .filter(file => SOURCE_EXTENSIONS.has(path.extname(file)))
    .filter(file => /\/packages\/Copal\/src\/(?:app|components\/views|components\/editor|hooks)\//.test(file) || /\/packages\/Copal\/src\/(?:openclank-codemirror|components\/EmojiPicker)\.(?:ts|tsx)$/.test(file))
    .filter(file => !JS_EXCLUDES.some(pattern => pattern.test(file)));
  const copalUi = walk(path.join(ROOT, 'packages', 'Copal', 'ui'))
    .filter(file => SOURCE_EXTENSIONS.has(path.extname(file)))
    .filter(file => !JS_EXCLUDES.some(pattern => pattern.test(file)));
  return [...new Set([...staticJs, ...copal, ...copalUi])].sort();
}

function extract() {
  const englishFile = path.join(I18N_DIR, 'en.json');
  const existingEnglish = readJson(englishFile, {});
  const collector = makeCollector(existingEnglish);

  for (const name of HTML_FILES) {
    const file = path.join(ROOT, name);
    if (fs.existsSync(file)) extractHtmlText(fs.readFileSync(file, 'utf8'), file, 1, collector);
  }
  for (const name of JSON_UI_FILES) {
    const file = path.join(ROOT, name);
    const data = readJson(file, {});
    for (const key of ['name', 'short_name', 'description']) collector.add(data[key], file, 1, 'json-metadata');
  }
  for (const file of sourceFiles()) extractJavaScript(file, collector);
  for (const root of PYTHON_ROOTS) {
    for (const file of walk(path.join(ROOT, root))) {
      if (file.endsWith('.py')) extractQuotedSource(file, collector);
    }
  }
  for (const file of walk(path.join(ROOT, 'packages', 'Copal', 'servo-shell', 'src'))) {
    if (file.endsWith('.rs')) extractQuotedSource(file, collector);
  }
  for (const file of [path.join(ROOT, 'static', 'style.css'), path.join(ROOT, 'packages', 'Copal', 'src', 'app', 'globals.css')]) {
    if (fs.existsSync(file)) extractCss(file, collector);
  }

  // Structured first-party content (TreeHouse lessons/awards, official docs)
  // is data in Python modules. Fold it into the live census so extract() does
  // not demote those keys to compatibility.
  try {
    const structured = JSON.parse(execFileSync('python3', [path.join(ROOT, 'scripts', 'i18n_structured_extract.py')], {
      encoding: 'utf8',
      cwd: ROOT,
    }));
    for (const record of structured.records || []) {
      const existing = collector.entries.get(record.key);
      if (existing) {
        if (!existing.locations.includes(...record.locations)) {
          for (const location of record.locations) if (!existing.locations.includes(location)) existing.locations.push(location);
        }
        continue;
      }
      collector.entries.set(record.key, {
        key: record.key,
        source: record.source,
        kind: record.kind || 'structured-prose',
        locations: [...(record.locations || [])],
      });
    }
  } catch (error) {
    process.stderr.write(`warning: structured extraction skipped: ${error.message}\n`);
  }

  const liveRecords = [...collector.entries.values()]
    .map(record => ({ ...record, status: 'live', locations: record.locations.sort() }))
    .sort((a, b) => a.key.localeCompare(b.key));
  // A source census is allowed to lose a render site while a compatibility
  // key is still present in shipped catalogs. Keep that key and make the
  // reason explicit in the ledger instead of silently deleting translations.
  const liveKeys = new Set(liveRecords.map(record => record.key));
  const compatibilityRecords = Object.entries(existingEnglish)
    .filter(([key]) => !liveKeys.has(key))
    .map(([key, source]) => ({
      key,
      source,
      kind: 'compatibility',
      status: 'compatibility',
      locations: [],
      reason: 'retained catalog key absent from current first-party census',
    }));
  const records = [...liveRecords, ...compatibilityRecords]
    .sort((a, b) => a.key.localeCompare(b.key));
  const english = Object.fromEntries(records.map(record => [record.key, record.source]));
  const ledger = {
    version: 2,
    source_count: records.length,
    current_source_count: liveRecords.length,
    compatibility_count: compatibilityRecords.length,
    source_hash: hashText(JSON.stringify(english)),
    roots: {
      html: HTML_FILES,
      javascript: sourceFiles().map(relative),
      server: PYTHON_ROOTS,
      native: ['packages/Copal/servo-shell/src'],
    },
    entries: records,
  };
  const registry = {
    version: 2,
    default_locale: 'en',
    locales: Object.fromEntries(Object.entries(LOCALES).map(([id, item]) => {
      const meta = {
        name: item.name,
        dir: item.dir,
        ...(item.catalog ? { catalog: item.catalog } : {}),
        ...(item.html_lang ? { html_lang: item.html_lang } : {}),
        ...(item.alias_of ? { alias_of: item.alias_of } : {}),
        ...(item.display_only ? { display_only: true } : {}),
      };
      if (item.target) meta.target = item.target;
      return [id, meta];
    })),
    aliases: existingRegistry.aliases || {
      zh: 'zh-Hans', 'zh-CN': 'zh-Hans', 'zh-SG': 'zh-Hans',
      in: 'id', 'pa-IN': 'pa-Guru',
    },
    do_not_auto_map: existingRegistry.do_not_auto_map || [],
  };

  writeJson(englishFile, english);
  for (const locale of Object.keys(LOCALES).filter(id => id !== 'en')) {
    // Display aliases (en-CA → fr) reuse the donor catalog file. Do not
    // duplicate French bytes into an en-CA.json catalog.
    if (LOCALES[locale]?.catalog && LOCALES[locale].catalog !== locale) continue;
    const file = path.join(I18N_DIR, `${locale}.json`);
    if (!fs.existsSync(file)) continue;
    const catalog = readJson(file, {});
    writeJson(file, Object.fromEntries(
      Object.keys(english).map(key => [
        key,
        typeof catalog[key] === 'string' && catalog[key].trim() ? catalog[key] : english[key],
      ]),
    ));
  }
  writeJson(path.join(I18N_DIR, 'ledger.json'), ledger);
  writeJson(path.join(I18N_DIR, 'registry.json'), registry);
  writeJson(path.join(I18N_DIR, 'brands.json'), { brands: BRANDS, stable_tokens: STABLE_TOKENS });
  if (!fs.existsSync(path.join(I18N_DIR, 'allowlist.json'))) {
    writeJson(path.join(I18N_DIR, 'allowlist.json'), {
      exact: [],
      patterns: [
        { pattern: '^(?:INFO|WARNING|ERROR|DEBUG|GET|POST|PUT|PATCH|DELETE)$', reason: 'protocol or diagnostic token' },
        { pattern: '^(?:https?://|/api/|/static/)', reason: 'URL or stable route' },
      ],
    });
  }

  process.stdout.write(`extracted=${records.length} hash=${ledger.source_hash}\n`);
}

function placeholderSet(value) {
  return [...value.matchAll(PLACEHOLDER)].map(match => match[1]).sort();
}

function donorRecords(payload, donorEnglish = {}) {
  if (payload && payload.schema === 1 && Array.isArray(payload.messages)) {
    return payload.messages
      .filter(record => record && typeof record.source === 'string')
      .map(record => ({
        key: typeof record.key === 'string' ? record.key : null,
        source: normalizeSource(record.source),
        context: typeof record.context === 'string' ? record.context.trim() : '',
        target: typeof record.target === 'string' ? record.target : undefined,
      }));
  }
  if (!payload || Array.isArray(payload) || typeof payload !== 'object') return [];
  return Object.entries(payload).map(([key, target]) => ({
    key,
    source: typeof donorEnglish[key] === 'string' ? normalizeSource(donorEnglish[key]) : null,
    context: '',
    target: typeof target === 'string' ? target : undefined,
  }));
}

function digest(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function importDonor(donor) {
  if (!donor) throw new Error('import-donor requires an explicit donor checkout path');
  const revision = '08eeb26a4e9b28190e6832f88b7d6e1d0a12646a';
  if (!fs.existsSync(donor)) throw new Error(`donor checkout does not exist: ${donor}`);
  const donorEnglishText = gitFile(donor, revision, 'static/i18n/en.json');
  if (!donorEnglishText) throw new Error(`unable to read donor revision ${revision}`);
  const donorEnglishPayload = JSON.parse(donorEnglishText);
  const donorEnglish = donorEnglishPayload?.messages
    ? Object.fromEntries(donorRecords(donorEnglishPayload).map(record => [record.key || record.source, record.source]))
    : donorEnglishPayload;
  let currentEnglish = readJson(path.join(I18N_DIR, 'en.json'), {});
  const baselineEnglishText = gitFile(ROOT, 'HEAD', 'static/i18n/en.json');
  const baselineEnglish = baselineEnglishText ? JSON.parse(baselineEnglishText) : {};
  // Recover any baseline source rows lost by the interrupted extractor before
  // resolving donor keys. Their prior targets are restored below per locale.
  currentEnglish = Object.fromEntries(Object.entries({ ...baselineEnglish, ...currentEnglish })
    .sort(([left], [right]) => left.localeCompare(right)));
  writeJson(path.join(I18N_DIR, 'en.json'), currentEnglish);
  const currentRegistry = readJson(path.join(I18N_DIR, 'registry.json'), {});
  const currentFiles = new Set(fs.readdirSync(I18N_DIR)
    .filter(name => /^[A-Za-z0-9-]+\.json$/u.test(name))
    .map(name => name.slice(0, -5)));
  const sourceToKeys = new Map();
  for (const [key, source] of Object.entries(currentEnglish)) {
    const normalized = normalizeSource(source);
    const keys = sourceToKeys.get(normalized) || [];
    keys.push(key);
    sourceToKeys.set(normalized, keys);
  }
  const aliases = { 'zh-CN': 'zh-Hans', 'zh-TW': 'zh-Hant' };
  const donorRegistryText = gitFile(donor, revision, 'static/i18n/registry.json');
  const donorRegistry = donorRegistryText ? JSON.parse(donorRegistryText) : {};
  const donorLocales = Object.keys(donorRegistry.locales || {})
    .filter(id => id !== 'en' && id !== 'brands');
  const changedSourceKeys = new Set();
  const baselineChangedSourceKeys = Object.keys(donorEnglish)
    .filter(key => baselineEnglish[key] !== undefined && donorEnglish[key] !== baselineEnglish[key])
    .sort();
  const currentLedger = readJson(path.join(I18N_DIR, 'ledger.json'), {});
  const liveKeys = new Set((currentLedger.entries || [])
    .filter(entry => entry.status === 'live' && typeof entry.key === 'string')
    .map(entry => entry.key));
  const baselineKeys = new Set(Object.keys(baselineEnglish));
  const censusAddedKeys = [...liveKeys].filter(key => !baselineKeys.has(key)).sort();
  const censusRemovedKeys = [...baselineKeys].filter(key => !liveKeys.has(key)).sort();
  const ambiguousContexts = [];
  const conflicts = [];
  const importedByLocale = {};
  const donorFiles = [];
  const mappedDonorLocales = new Set();
  const restoredByLocale = {};

  for (const donorLocale of donorLocales) {
    const locale = aliases[donorLocale] || donorLocale;
    if (locale === 'en') continue;
    const committedPath = `static/i18n/${donorLocale}.json`;
    const committedText = gitFile(donor, revision, committedPath);
    const workingPath = path.join(donor, 'static', 'i18n', `${donorLocale}.json`);
    const workingText = fs.existsSync(workingPath) ? fs.readFileSync(workingPath, 'utf8') : null;
    if (!committedText && !workingText) continue;
    donorFiles.push({
      locale: donorLocale,
      mapped_locale: locale,
      committed_revision: revision,
      committed_sha256: committedText ? digest(committedText) : null,
      working_sha256: workingText ? digest(workingText) : null,
    });
    mappedDonorLocales.add(locale);
    const committed = donorRecords(committedText ? JSON.parse(committedText) : {}, donorEnglish);
    const committedByKey = new Map();
    for (const row of committed) {
      if (row.key && row.source && currentEnglish[row.key] !== undefined) {
        if (normalizeSource(currentEnglish[row.key]) === row.source) committedByKey.set(row.key, row);
        else changedSourceKeys.add(row.key);
      }
    }
    const working = donorRecords(workingText ? JSON.parse(workingText) : {}, donorEnglish);
    const workingBySource = new Map();
    for (const row of working) {
      if (!row.source || !row.target) continue;
      const keys = sourceToKeys.get(row.source) || [];
      if (keys.length !== 1) {
        if (keys.length > 1) ambiguousContexts.push({ locale, source: row.source, keys: [...keys].sort() });
        continue;
      }
      const prior = workingBySource.get(row.source);
      if (!prior || row.context < prior.context) workingBySource.set(row.source, row);
    }
    const file = path.join(I18N_DIR, `${locale}.json`);
    const currentCatalog = readJson(file, {});
    const baselineCatalogText = gitFile(ROOT, 'HEAD', `static/i18n/${locale}.json`);
    const baselineCatalog = baselineCatalogText ? JSON.parse(baselineCatalogText) : {};
    const output = {};
    let imported = 0;
    let restored = 0;
    for (const [key, source] of Object.entries(currentEnglish)) {
      let existing = currentCatalog[key];
      // The interrupted extractor may have dropped a current translation. If
      // the baseline source is unchanged, recover its target before applying
      // donor candidates; this keeps current authored work authoritative.
      if ((!existing || existing === source)
          && baselineEnglish[key] === source
          && translationIsStructurallyValid(source, baselineCatalog[key])
          && baselineCatalog[key] !== source) {
        existing = baselineCatalog[key];
        restored += 1;
      }
      const committed = committedByKey.get(key);
      const workingRow = workingBySource.get(normalizeSource(source));
      const donorTarget = committed?.target && committed.target !== committed.source
        ? committed.target
        : workingRow?.target && workingRow.target !== workingRow.source ? workingRow.target : undefined;
      const donorKind = committed?.target && committed.target !== committed.source ? 'committed'
        : donorTarget ? 'working-source-context' : null;
      // A malformed donor row is recorded by omission from the import and
      // falls back to the source. It must never make a current key disappear.
      const usableDonorTarget = donorTarget && translationIsStructurallyValid(source, donorTarget)
        ? donorTarget : undefined;
      const usableDonorKind = usableDonorTarget ? donorKind : null;
      if (typeof existing === 'string' && existing.trim() && existing !== source) {
        output[key] = existing;
        if (usableDonorTarget && usableDonorTarget !== existing) {
          conflicts.push({ locale, key, source, retained: existing, donor: usableDonorTarget, donor_kind: usableDonorKind });
        }
      } else if (usableDonorTarget) {
        output[key] = usableDonorTarget;
        imported += 1;
      } else {
        output[key] = source;
      }
    }
    writeJson(file, output);
    importedByLocale[locale] = { imported, restored, keys: Object.keys(output).length };
    restoredByLocale[locale] = restored;
  }

  // Existing translated catalogs remain active; other registry choices retain
  // an explicit English fallback so no language silently disappears.
  const locales = { ...(currentRegistry.locales || {}) };
  for (const [locale, metadata] of Object.entries(locales)) {
    const active = locale !== 'en' && (mappedDonorLocales.has(locale) || currentFiles.has(locale));
    locales[locale] = {
      name: metadata.name || locale,
      dir: metadata.dir || 'ltr',
      catalog: active ? locale : 'en',
    };
  }
  writeJson(path.join(I18N_DIR, 'registry.json'), {
    version: 2,
    default_locale: currentRegistry.default_locale || 'en',
    locales,
    aliases: currentRegistry.aliases || {},
    do_not_auto_map: currentRegistry.do_not_auto_map || [],
  });
  donorFiles.sort((a, b) => a.locale.localeCompare(b.locale));
  const provenance = {
    schema: 1,
    operation: 'translation-recovery',
    donor: {
      source: 'https://github.com/odysseus-dev/odysseus',
      checkout_scope: 'Local source checkout; absolute operator path omitted from published provenance',
      revision,
      license: 'AGPL-3.0',
      license_sha256: (() => {
        const text = gitFile(donor, revision, 'LICENSE');
        return text ? digest(text) : null;
      })(),
      files: donorFiles,
    },
    current: {
      english_sha256: digest(JSON.stringify(currentEnglish)),
      english_keys: Object.keys(currentEnglish).length,
    },
    current_source_census: {
      baseline: 'HEAD:static/i18n/en.json',
      baseline_keys: baselineKeys.size,
      live_keys: liveKeys.size,
      added_keys: censusAddedKeys,
      removed_keys: censusRemovedKeys,
      added_count: censusAddedKeys.length,
      removed_count: censusRemovedKeys.length,
    },
    mapping: aliases,
    imported_by_locale: Object.fromEntries(Object.entries(importedByLocale).sort()),
    restored_current_by_locale: Object.fromEntries(Object.entries(restoredByLocale).sort()),
    changed_source_keys: [...changedSourceKeys].sort(),
    baseline_changed_source_keys: baselineChangedSourceKeys,
    ambiguous_contexts: [...new Map(ambiguousContexts.map(row => [JSON.stringify(row), row])).values()]
      .sort((a, b) => `${a.locale}\0${a.source}`.localeCompare(`${b.locale}\0${b.source}`)),
    conflicts: conflicts.sort((a, b) => `${a.locale}\0${a.key}`.localeCompare(`${b.locale}\0${b.key}`)),
    counts: {
      changed_sources: changedSourceKeys.size,
      baseline_changed_sources: baselineChangedSourceKeys.length,
      ambiguous_contexts: ambiguousContexts.length,
      conflicts: conflicts.length,
      restored_current: Object.values(restoredByLocale).reduce((sum, count) => sum + count, 0),
    },
  };
  writeJson(path.join(I18N_DIR, 'provenance.json'), provenance);
  process.stdout.write(`donor-imported locales=${Object.keys(importedByLocale).length} conflicts=${conflicts.length} changed_sources=${changedSourceKeys.size}\n`);
}

function validate() {
  const english = readJson(path.join(I18N_DIR, 'en.json'), null);
  if (!english || typeof english !== 'object' || Array.isArray(english)) throw new Error('missing or invalid en.json');
  const englishKeys = Object.keys(english).sort();
  const errors = [];
  const warnings = [];
  const untranslatedAllowed = new Set([...BRANDS, ...STABLE_TOKENS, 'English']);

  const validatedCatalogFiles = new Set();
  for (const [locale, meta] of Object.entries(LOCALES)) {
    const catalogName = meta.catalog || locale;
    if (catalogName === 'en') {
      process.stdout.write(`${locale}: fallback catalog=en dir=${meta.dir}\n`);
      continue;
    }
    const file = path.join(I18N_DIR, `${catalogName}.json`);
    // Display aliases such as en-CA share the donor catalog (fr). Validate the
    // donor file once and report the alias as covered by it.
    if (validatedCatalogFiles.has(catalogName)) {
      process.stdout.write(`${locale}: alias catalog=${catalogName} dir=${meta.dir}\n`);
      continue;
    }
    if (!fs.existsSync(file)) {
      errors.push(`${locale}: missing catalog ${catalogName}.json`);
      continue;
    }
    const catalog = readJson(file, null);
    if (!catalog || typeof catalog !== 'object' || Array.isArray(catalog)) {
      errors.push(`${locale}: invalid catalog`);
      continue;
    }
    validatedCatalogFiles.add(catalogName);
    const keys = Object.keys(catalog).sort();
    const missing = englishKeys.filter(key => !(key in catalog));
    const extra = keys.filter(key => !(key in english));
    if (missing.length) errors.push(`${locale}: ${missing.length} missing keys: ${missing.slice(0, 8).join(', ')}`);
    if (extra.length) errors.push(`${locale}: ${extra.length} extra keys: ${extra.slice(0, 8).join(', ')}`);

    for (const key of englishKeys) {
      const source = english[key];
      const target = catalog[key];
      if (typeof target !== 'string' || !target.trim()) {
        errors.push(`${locale}:${key}: empty or non-string value`);
        continue;
      }
      if (BIDI_CONTROLS.test(target)) errors.push(`${locale}:${key}: forbidden bidi control`);
      if (/<\/?[a-z][^>]*>/i.test(target)) errors.push(`${locale}:${key}: HTML is forbidden`);
      if (JSON.stringify(placeholderSet(source)) !== JSON.stringify(placeholderSet(target))) {
        errors.push(`${locale}:${key}: placeholder mismatch ${JSON.stringify(placeholderSet(source))} != ${JSON.stringify(placeholderSet(target))}`);
      }
      for (const token of [...BRANDS, ...STABLE_TOKENS]) {
        if (source.includes(token) && !target.includes(token)) errors.push(`${locale}:${key}: changed locked token ${token}`);
      }
      if (locale !== 'en' && target === source && !untranslatedAllowed.has(source) && /[A-Za-z]{3}/.test(source)) {
        warnings.push(`${locale}:${key}: unchanged English: ${source}`);
      }
    }
    process.stdout.write(`${locale}: keys=${keys.length} catalog=${catalogName} dir=${meta.dir}\n`);
  }

  for (const warning of warnings.slice(0, 80)) process.stderr.write(`warning: ${warning}\n`);
  if (warnings.length > 80) process.stderr.write(`warning: ... ${warnings.length - 80} more unchanged entries\n`);
  if (errors.length) {
    for (const error of errors.slice(0, 120)) process.stderr.write(`error: ${error}\n`);
    if (errors.length > 120) process.stderr.write(`error: ... ${errors.length - 120} more\n`);
    throw new Error(`catalog validation failed with ${errors.length} error(s)`);
  }
  process.stdout.write(`validated locales=${Object.keys(LOCALES).length} keys=${englishKeys.length} warnings=${warnings.length}\n`);
}

function batches(entries, maxEntries = 140, maxChars = 18000) {
  const result = [];
  let batch = [];
  let chars = 0;
  for (const entry of entries) {
    const size = entry[0].length + entry[1].length + 8;
    if (batch.length && (batch.length >= maxEntries || chars + size > maxChars)) {
      result.push(batch);
      batch = [];
      chars = 0;
    }
    batch.push(entry);
    chars += size;
  }
  if (batch.length) result.push(batch);
  return result;
}

async function ollamaTranslate(locale, entries, attempt = 1) {
  const meta = LOCALES[locale];
  const replacements = [];
  const input = Object.fromEntries(entries
    .map(([key, source]) => [key, protectForMachineTranslation(source, replacements, index => `🔒${index}🔒`)]));
  const relevantLocks = [...BRANDS, ...STABLE_TOKENS]
    .filter(token => entries.some(([, source]) => source.includes(token)));
  const prompt = [
    `Translate this JSON object from English to ${meta.target} for the Open Clank software UI.`,
    'Return exactly one valid JSON object with identical keys and string values. No markdown or commentary.',
    'Preserve every opaque token matching 🔒<number>🔒 exactly.',
    'Translate complete meaning naturally for buttons, dialogs, errors, settings, accessibility labels, notes, calendar, email, AI tools, and desktop software.',
    'Do not translate these exact locked tokens when present: ' + relevantLocks.join(', '),
    'Do not add HTML or Unicode bidi control characters.',
    'Keep concise labels concise. Use a consistent respectful product voice.',
    `Input JSON: ${JSON.stringify(input)}`,
  ].join('\n');

  const response = await fetch('http://127.0.0.1:11434/api/chat', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: 'translategemma:12b',
      stream: false,
      format: 'json',
      messages: [{ role: 'user', content: prompt }],
      options: { temperature: 0 },
    }),
  });
  if (!response.ok) throw new Error(`Ollama HTTP ${response.status}`);
  const payload = await response.json();
  const content = payload?.message?.content;
  let parsed;
  try { parsed = JSON.parse(content); } catch (error) {
    if (entries.length > 1) {
      const midpoint = Math.ceil(entries.length / 2);
      return {
        ...(await ollamaTranslate(locale, entries.slice(0, midpoint), attempt + 1)),
        ...(await ollamaTranslate(locale, entries.slice(midpoint), attempt + 1)),
      };
    }
    if (attempt < 5) return ollamaTranslate(locale, entries, attempt + 1);
    throw new Error(`${locale}: invalid JSON response: ${error.message}: ${String(content).slice(0, 300)}`);
  }
  const restored = Object.fromEntries(entries.map(([key]) => {
    let value = parsed[key];
    if (typeof value === 'string') {
      for (const [token, original] of replacements) value = value.split(token).join(original);
    }
    return [key, value];
  }));
  const result = Object.fromEntries(entries
    .filter(([key, source]) => translationIsStructurallyValid(source, restored[key]))
    .map(([key]) => [key, restored[key]]));
  const missing = entries.filter(([key]) => typeof result[key] !== 'string');
  if (missing.length) {
    if (attempt >= 5) {
      const details = missing.slice(0, 4).map(([key]) => `${key}=${JSON.stringify(restored[key])}`).join(', ');
      throw new Error(`${locale}: response missing or structurally invalid for ${missing.length} key(s): ${details}`);
    }
    Object.assign(result, await ollamaTranslate(locale, missing, attempt + 1));
  }
  return result;
}

async function translateLocale(locale) {
  if (!LOCALES[locale] || locale === 'en') throw new Error(`unsupported target locale: ${locale}`);
  const english = readJson(path.join(I18N_DIR, 'en.json'), null);
  if (!english) throw new Error('run extract first');
  const file = path.join(I18N_DIR, `${locale}.json`);
  const existing = readJson(file, {});
  const pending = Object.entries(english)
    .filter(([key, source]) => !translationIsStructurallyValid(source, existing[key]));
  const work = batches(pending);
  process.stdout.write(`${locale}: pending=${pending.length} batches=${work.length}\n`);
  for (let index = 0; index < work.length; index += 1) {
    const translated = await ollamaTranslate(locale, work[index]);
    Object.assign(existing, translated);
    const ordered = Object.fromEntries(Object.keys(english).map(key => [key, existing[key]]));
    writeJson(file, ordered);
    process.stdout.write(`${locale}: batch=${index + 1}/${work.length} keys=${Object.keys(translated).length}\n`);
  }
}

const GOOGLE_CODES = {
  'zh-Hans': 'zh-CN', ja: 'ja', ko: 'ko', es: 'es', hi: 'hi', ar: 'ar', ru: 'ru',
  pt: 'pt', id: 'id', 'pa-Guru': 'pa', bn: 'bn', sw: 'sw', ur: 'ur', fa: 'fa',
};

function protectForMachineTranslation(value, replacements, tokenFor = index => `ZXQLOCK${index}QXZ`) {
  let protectedValue = value;
  const locks = [...BRANDS, ...STABLE_TOKENS].sort((a, b) => b.length - a.length);
  for (const lock of locks) {
    if (!protectedValue.includes(lock)) continue;
    const token = tokenFor(replacements.length);
    replacements.push([token, lock]);
    protectedValue = protectedValue.split(lock).join(token);
  }
  protectedValue = protectedValue.replace(PLACEHOLDER, placeholder => {
    const token = tokenFor(replacements.length);
    replacements.push([token, placeholder]);
    return token;
  });
  return protectedValue;
}

async function googleTranslateBatch(locale, entries, attempt = 1) {
  const separator = 'ZXQ987654321QXZ';
  const replacements = [];
  const input = entries.map(([, value]) => protectForMachineTranslation(value, replacements)).join(`\n${separator}\n`);
  const query = new URLSearchParams({ client: 'gtx', sl: 'en', tl: GOOGLE_CODES[locale], dt: 't', q: input });
  let response;
  try {
    response = await fetch(`https://translate.googleapis.com/translate_a/single?${query}`);
  } catch (error) {
    if (attempt < 4) return googleTranslateBatch(locale, entries, attempt + 1);
    throw error;
  }
  if (!response.ok) {
    if (attempt < 4) return googleTranslateBatch(locale, entries, attempt + 1);
    throw new Error(`${locale}: Google Translate HTTP ${response.status}`);
  }
  const payload = await response.json();
  let output = (payload?.[0] || []).map(part => part?.[0] || '').join('');
  output = output.replace(new RegExp(BIDI_CONTROLS.source, 'gu'), '');
  for (const [token, original] of replacements) output = output.split(token).join(original);
  const values = output.split(new RegExp(`\\s*${separator}\\s*`, 'u'));
  if (values.length !== entries.length) {
    if (entries.length > 1) {
      const midpoint = Math.ceil(entries.length / 2);
      return {
        ...(await googleTranslateBatch(locale, entries.slice(0, midpoint), attempt + 1)),
        ...(await googleTranslateBatch(locale, entries.slice(midpoint), attempt + 1)),
      };
    }
    throw new Error(`${locale}: translation boundary mismatch for ${entries[0][0]}`);
  }
  return Object.fromEntries(entries.map(([key], index) => [key, values[index].trim()]));
}

async function translateLocaleGoogle(locale) {
  const english = readJson(path.join(I18N_DIR, 'en.json'), null);
  if (!english || !GOOGLE_CODES[locale]) throw new Error(`unsupported Google translation locale: ${locale}`);
  const file = path.join(I18N_DIR, `${locale}.json`);
  const existing = readJson(file, {});
  const pending = Object.entries(english).filter(([key]) => typeof existing[key] !== 'string' || !existing[key].trim());
  const work = batches(pending, 90, 4200);
  process.stdout.write(`${locale}: pending=${pending.length} google_batches=${work.length}\n`);
  for (let index = 0; index < work.length; index += 1) {
    Object.assign(existing, await googleTranslateBatch(locale, work[index]));
    writeJson(file, Object.fromEntries(Object.keys(english).map(key => [key, existing[key]])));
    process.stdout.write(`${locale}: google_batch=${index + 1}/${work.length}\n`);
  }
}

function translationIsStructurallyValid(source, target) {
  if (typeof target !== 'string' || !target.trim() || BIDI_CONTROLS.test(target) || /<\/?[a-z][^>]*>/i.test(target)) return false;
  if (JSON.stringify(placeholderSet(source)) !== JSON.stringify(placeholderSet(target))) return false;
  return [...BRANDS, ...STABLE_TOKENS].every(token => !source.includes(token) || target.includes(token));
}

async function repairLocaleGoogle(locale) {
  const english = readJson(path.join(I18N_DIR, 'en.json'), {});
  const file = path.join(I18N_DIR, `${locale}.json`);
  const existing = readJson(file, {});
  let removed = 0;
  for (const [key, source] of Object.entries(english)) {
    if (!translationIsStructurallyValid(source, existing[key])) { delete existing[key]; removed += 1; }
  }
  writeJson(file, existing);
  process.stdout.write(`${locale}: structurally_invalid=${removed}\n`);
  await translateLocaleGoogle(locale);
}

function generateLocalizedManifests() {
  const sourceManifest = readJson(path.join(ROOT, 'static', 'manifest.json'), {});
  const english = readJson(path.join(I18N_DIR, 'en.json'), {});
  const descriptionKey = Object.keys(english).find(key => english[key] === sourceManifest.description);
  const cache = new Map();
  const loadCatalog = name => {
    if (!cache.has(name)) cache.set(name, readJson(path.join(I18N_DIR, `${name}.json`), {}));
    return cache.get(name);
  };
  for (const [locale, meta] of Object.entries(LOCALES)) {
    const catalogName = meta.catalog || locale;
    const catalog = catalogName === 'en' ? english : loadCatalog(catalogName);
    writeJson(path.join(ROOT, 'static', `manifest.${locale}.json`), {
      ...sourceManifest,
      lang: meta.html_lang || catalogName,
      name: meta.name,
      description: catalog[descriptionKey] || sourceManifest.description,
    });
  }
  process.stdout.write(`generated manifests=${Object.keys(LOCALES).length}\n`);
}

async function main() {
  const [command = 'validate', locale] = process.argv.slice(2);
  if (command === 'extract') extract();
  else if (command === 'import-donor') importDonor(locale);
  else if (command === 'validate') validate();
  else if (command === 'translate') await translateLocale(locale);
  else if (command === 'translate-all') {
    for (const id of Object.keys(LOCALES).filter(id => id !== 'en')) await translateLocale(id);
  } else if (command === 'translate-google') await translateLocaleGoogle(locale);
  else if (command === 'translate-google-all') {
    const queue = Object.keys(GOOGLE_CODES);
    await Promise.all(Array.from({ length: 3 }, async () => {
      while (queue.length) await translateLocaleGoogle(queue.shift());
    }));
  } else if (command === 'repair-google-all') {
    for (const id of Object.keys(GOOGLE_CODES)) await repairLocaleGoogle(id);
  } else if (command === 'manifests') generateLocalizedManifests();
  else if (command === 'freeze' || command === 'structured-extract') {
    const script = path.join(ROOT, 'scripts', command === 'freeze' ? 'i18n_freeze.py' : 'i18n_structured_extract.py');
    const extra = process.argv.slice(3);
    execFileSync('python3', [script, ...extra], { stdio: 'inherit', cwd: ROOT });
  } else {
    throw new Error('usage: node scripts/i18n-catalog.mjs extract|import-donor DONOR|validate|freeze|structured-extract|translate LOCALE|translate-all|translate-google LOCALE|translate-google-all|repair-google-all|manifests');
  }
}

main().catch(error => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
