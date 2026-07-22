const STORAGE_KEY = 'openclank.locale';
const ATTRIBUTES = ['title', 'placeholder', 'aria-label', 'aria-description', 'alt'];
const USER_CONTENT_SELECTOR = [
  '[data-user-content]', '.msg .body', '.document-content', '.document-title', '.note-editor', '.note-content-preview', '.note-title',
  '.memory-item-content', '.session-title', '.email-reader-body', '.email-subject', '.email-sender',
  '.research-job-report-body', '.task-log-row-body',
].join(',');
const SKIP_SELECTOR = [
  'script', 'style', 'code', 'pre', 'textarea', '[contenteditable="true"]', '[data-i18n-skip]', '[data-user-content]',
  '.msg .body', '.document-content', '.document-title', '.note-editor', '.note-content-preview', '.note-title',
  '.memory-item-content', '.session-title', '.email-reader-body', '.email-subject', '.email-sender',
  '.research-job-report-body', '.task-log-row-body',
].join(',');
const PROMPTS = {
  'zh-Hans': ['是否将语言切换为简体中文？', '切换', '暂不'],
  ja: ['言語を日本語に変更しますか？', '変更する', '今はしない'],
  ko: ['언어를 한국어로 변경하시겠습니까?', '변경', '나중에'],
  es: ['¿Quieres cambiar el idioma a español?', 'Cambiar', 'Ahora no'],
  hi: ['क्या आप भाषा हिन्दी में बदलना चाहेंगे?', 'बदलें', 'अभी नहीं'],
  ar: ['هل تريد تغيير اللغة إلى العربية؟', 'تغيير', 'ليس الآن'],
  ru: ['Хотите переключить язык на русский?', 'Переключить', 'Не сейчас'],
  pt: ['Gostaria de mudar o idioma para português?', 'Mudar', 'Agora não'],
  id: ['Apakah Anda ingin mengubah bahasa ke Bahasa Indonesia?', 'Ubah', 'Jangan sekarang'],
  'pa-Guru': ['ਕੀ ਤੁਸੀਂ ਭਾਸ਼ਾ ਪੰਜਾਬੀ ਵਿੱਚ ਬਦਲਣਾ ਚਾਹੁੰਦੇ ਹੋ?', 'ਬਦਲੋ', 'ਹੁਣ ਨਹੀਂ'],
  bn: ['আপনি কি ভাষা বাংলায় পরিবর্তন করতে চান?', 'পরিবর্তন করুন', 'এখন নয়'],
  sw: ['Je, ungependa kubadilisha lugha iwe Kiswahili?', 'Badilisha', 'Si sasa'],
  ur: ['کیا آپ زبان اردو میں تبدیل کرنا چاہیں گے؟', 'تبدیل کریں', 'ابھی نہیں'],
  fa: ['آیا می‌خواهید زبان را به فارسی تغییر دهید؟', 'تغییر', 'فعلاً نه'],
};
const LANGUAGE_CHANGED = {
  en: 'Language changed to English.', 'zh-Hans': '语言已切换为简体中文。', ja: '言語を日本語に変更しました。',
  ko: '언어가 한국어로 변경되었습니다.', es: 'El idioma cambió a español.', hi: 'भाषा हिन्दी में बदल दी गई है।',
  ar: 'تم تغيير اللغة إلى العربية.', ru: 'Язык изменён на русский.', pt: 'O idioma foi alterado para português.',
  id: 'Bahasa diubah ke Bahasa Indonesia.', 'pa-Guru': 'ਭਾਸ਼ਾ ਪੰਜਾਬੀ ਵਿੱਚ ਬਦਲ ਦਿੱਤੀ ਗਈ ਹੈ।', bn: 'ভাষা বাংলায় পরিবর্তন করা হয়েছে।',
  sw: 'Lugha imebadilishwa kuwa Kiswahili.', ur: 'زبان اردو میں تبدیل کر دی گئی ہے۔', fa: 'زبان به فارسی تغییر کرد.',
};
const CSS_MESSAGES = {
  '--i18n-css-copied': '✓ Copied',
  '--i18n-css-editing': 'EDITING',
  '--i18n-css-enabled': 'Enabled',
  '--i18n-css-drop-to-attach': 'Drop to attach',
  '--i18n-css-write-email': 'Write your email…',
  '--i18n-css-planning-goal': 'AI is planning your goal…',
  '--i18n-css-no-title': 'No title',
  '--i18n-css-table-malformed': '⚠ This table has formatting issues — showing raw source',
};

let registry;
let english = {};
let catalog = {};
let locale = 'en';
let observer;
let applying = false;
const originals = new WeakMap();
const rendered = new WeakMap();
const originalAttrs = new WeakMap();
const renderedAttrs = new WeakMap();
let templates = [];
const nativeDialogs = {
  alert: window.alert.bind(window),
  confirm: window.confirm.bind(window),
  prompt: window.prompt.bind(window),
};

const fetchJson = async name => {
  const response = await fetch(`/static/i18n/${name}.json`, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`Unable to load language resource: ${name}`);
  return response.json();
};

const normalize = value => String(value ?? '').replace(/[\t\n ]+/g, ' ').trim();
const escapeRegex = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function rebuildIndex() {
  const exact = new Map();
  templates = [];
  for (const [key, source] of Object.entries(english)) {
    const target = catalog[key] ?? source;
    exact.set(normalize(source), target);
    if (/\{\d+\}/.test(source)) {
      const names = [];
      const pattern = escapeRegex(normalize(source)).replace(/\\\{(\d+)\\\}/g, (_, name) => {
        names.push(name);
        return '(.+?)';
      });
      templates.push({ regex: new RegExp(`^${pattern}$`, 'u'), names, target });
    }
  }
  window.__openClankI18nExact = exact;
}

function translateValue(value) {
  const normalized = normalize(value);
  if (!normalized || locale === 'en') return value;
  const exact = window.__openClankI18nExact.get(normalized);
  if (exact != null) return exact;
  for (const template of templates) {
    const match = normalized.match(template.regex);
    if (!match) continue;
    const values = {};
    template.names.forEach((name, index) => { values[name] = match[index + 1]; });
    return template.target.replace(/\{(\d+)\}/g, (_, name) => values[name] ?? `{${name}}`);
  }
  return value;
}

function shouldSkip(node) {
  const parent = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  return !parent || parent.closest(SKIP_SELECTOR);
}

function translateText(node) {
  if (shouldSkip(node) || !normalize(node.nodeValue)) return;
  const current = node.nodeValue;
  const last = rendered.get(node);
  if (!originals.has(node) || current !== last) originals.set(node, current);
  const source = originals.get(node);
  const translatedValue = locale === 'en' ? source : translateValue(source);
  const leading = source.match(/^\s*/u)?.[0] || '';
  const trailing = source.match(/\s*$/u)?.[0] || '';
  const translated = locale === 'en' || translatedValue === source
    ? translatedValue
    : `${leading}${translatedValue}${trailing}`;
  if (translated !== current) node.nodeValue = translated;
  rendered.set(node, translated);
}

function translateElement(element) {
  if (shouldSkip(element)) return;
  const originalsForElement = originalAttrs.get(element) || {};
  const renderedForElement = renderedAttrs.get(element) || {};
  for (const attribute of ATTRIBUTES) {
    if (!element.hasAttribute(attribute)) continue;
    const current = element.getAttribute(attribute);
    if (!(attribute in originalsForElement) || current !== renderedForElement[attribute]) originalsForElement[attribute] = current;
    const source = originalsForElement[attribute];
    const translated = locale === 'en' ? source : translateValue(source);
    if (translated !== current) element.setAttribute(attribute, translated);
    renderedForElement[attribute] = translated;
  }
  originalAttrs.set(element, originalsForElement);
  renderedAttrs.set(element, renderedForElement);
}

function translateTree(root = document.documentElement) {
  applying = true;
  try {
    if (root.nodeType === Node.TEXT_NODE) translateText(root);
    if (root.nodeType === Node.ELEMENT_NODE) translateElement(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (walker.currentNode.nodeType === Node.TEXT_NODE) translateText(walker.currentNode);
      else translateElement(walker.currentNode);
    }
    if (root.nodeType === Node.ELEMENT_NODE) {
      if (root.matches(USER_CONTENT_SELECTOR) && !root.hasAttribute('dir')) root.setAttribute('dir', 'auto');
      root.querySelectorAll(USER_CONTENT_SELECTOR).forEach(element => {
        if (!element.hasAttribute('dir')) element.setAttribute('dir', 'auto');
      });
    }
  } finally {
    queueMicrotask(() => { applying = false; });
  }
}

function syncLanguageControls() {
  document.querySelectorAll('[data-language-select]').forEach(select => { select.value = locale; });
  for (const [property, source] of Object.entries(CSS_MESSAGES)) {
    document.documentElement.style.setProperty(property, JSON.stringify(locale === 'en' ? source : translateValue(source)));
  }
}

async function setLocale(next, { persist = true } = {}) {
  if (!registry?.locales[next]) next = 'en';
  const nextCatalog = next === 'en' ? english : await fetchJson(next);
  locale = next;
  catalog = nextCatalog;
  rebuildIndex();
  document.documentElement.lang = next;
  document.documentElement.dir = registry.locales[next].dir;
  const manifest = document.querySelector('link[rel="manifest"]');
  if (manifest && !manifest.href.startsWith('blob:')) manifest.href = `/static/manifest.${next}.json`;
  if (persist) localStorage.setItem(STORAGE_KEY, next);
  translateTree();
  syncLanguageControls();
  if (persist) {
    let status = document.getElementById('i18n-language-status');
    if (!status) {
      status = document.createElement('div');
      status.id = 'i18n-language-status';
      status.setAttribute('role', 'status');
      status.setAttribute('aria-live', 'polite');
      status.style.cssText = 'position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0';
      document.body.appendChild(status);
    }
    status.lang = next;
    status.dir = registry.locales[next].dir;
    status.textContent = LANGUAGE_CHANGED[next];
  }
  document.dispatchEvent(new CustomEvent('openclank:languagechange', { detail: { locale: next } }));
  return next;
}

function browserLocale() {
  for (const requested of navigator.languages || [navigator.language]) {
    if (!requested) continue;
    if (registry.do_not_auto_map.some(blocked => requested.toLowerCase().startsWith(blocked.toLowerCase()))) continue;
    if (registry.locales[requested]) return requested;
    if (registry.aliases[requested]) return registry.aliases[requested];
    const base = requested.split('-')[0];
    if (registry.locales[base]) return base;
    if (registry.aliases[base]) return registry.aliases[base];
  }
  return 'en';
}

function offerLocale(candidate) {
  if (!PROMPTS[candidate] || localStorage.getItem(`${STORAGE_KEY}.prompted.${candidate}`)) return;
  localStorage.setItem(`${STORAGE_KEY}.prompted.${candidate}`, '1');
  if (!document.getElementById('i18n-offer-style')) {
    const style = document.createElement('style');
    style.id = 'i18n-offer-style';
    style.textContent = '.i18n-offer{position:fixed;inset:0;z-index:var(--i18n-offer-z,100000);display:grid;place-items:center;padding:20px;background:#0009}.i18n-offer-card{width:min(420px,100%);box-sizing:border-box;padding:22px;border:1px solid #666;border-radius:14px;background:#181818;color:#f5f5f5;box-shadow:0 18px 60px #0006}.i18n-offer-card p{margin:0 0 18px;font:16px/1.5 system-ui,sans-serif}.i18n-offer-card div{display:flex;justify-content:flex-end;gap:9px}.i18n-offer-card button{min-height:38px;padding:7px 14px;border:1px solid #666;border-radius:8px;background:transparent;color:inherit;cursor:pointer}.i18n-offer-card button[data-accept]{background:#b83232;border-color:#b83232;color:#fff}.i18n-offer-card button:focus-visible{outline:2px solid #e55;outline-offset:2px}';
    document.head.appendChild(style);
  }
  const [message, acceptLabel, declineLabel] = PROMPTS[candidate];
  const modal = document.createElement('div');
  modal.className = 'i18n-offer';
  modal.dir = registry.locales[candidate].dir;
  modal.lang = candidate;
  modal.innerHTML = `<div class="i18n-offer-card" role="dialog" aria-modal="true" aria-labelledby="i18n-offer-title"><p id="i18n-offer-title"></p><div><button type="button" data-accept></button><button type="button" data-decline></button></div></div>`;
  modal.querySelector('p').textContent = message;
  modal.querySelector('[data-accept]').textContent = acceptLabel;
  modal.querySelector('[data-decline]').textContent = declineLabel;
  const priorFocus = document.activeElement;
  const close = () => { modal.remove(); priorFocus?.focus?.(); };
  modal.querySelector('[data-accept]').addEventListener('click', async () => { await setLocale(candidate); close(); });
  modal.querySelector('[data-decline]').addEventListener('click', close);
  modal.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key !== 'Tab') return;
    const buttons = [...modal.querySelectorAll('button')];
    const index = buttons.indexOf(document.activeElement);
    const next = event.shiftKey ? (index <= 0 ? buttons.length - 1 : index - 1) : (index + 1) % buttons.length;
    event.preventDefault();
    buttons[next].focus();
  });
  document.body.appendChild(modal);
  modal.querySelector('[data-accept]').focus();
}

async function init() {
  [registry, english] = await Promise.all([fetchJson('registry'), fetchJson('en')]);
  document.addEventListener('change', event => {
    if (event.target.matches('[data-language-select]')) {
      setLocale(event.target.value).catch(error => {
        console.error('[i18n]', error);
        syncLanguageControls();
      });
    }
  });
  const saved = localStorage.getItem(STORAGE_KEY);
  await setLocale(saved && registry.locales[saved] ? saved : 'en', { persist: false });
  window.alert = message => nativeDialogs.alert(translateValue(message));
  window.confirm = message => nativeDialogs.confirm(translateValue(message));
  window.prompt = (message, defaultValue) => nativeDialogs.prompt(translateValue(message), defaultValue);
  observer = new MutationObserver(records => {
    if (applying) return;
    for (const record of records) {
      if (record.type === 'characterData') translateText(record.target);
      else if (record.type === 'attributes') translateElement(record.target);
      else record.addedNodes.forEach(translateTree);
    }
  });
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRIBUTES });
  if (!saved) offerLocale(browserLocale());
}

const ready = init().catch(error => console.error('[i18n]', error));
const interpolate = (value, parameters = {}) => String(value).replace(/\{([A-Za-z_][A-Za-z0-9_]*|\d+)\}/g, (placeholder, name) => (
  Object.hasOwn(parameters, name) ? String(parameters[name]) : placeholder
));
window.openClankI18n = {
  ready,
  get locale() { return locale; },
  get locales() { return registry?.locales || {}; },
  setLocale,
  t(value) { return translateValue(value); },
  key(key, parameters) { return interpolate(catalog[key] ?? english[key] ?? key, parameters); },
  plural(count, forms) { const category = new Intl.PluralRules(locale).select(count); return forms[category] ?? forms.other; },
  formatNumber(value, options) { return new Intl.NumberFormat(locale, options).format(value); },
  formatDate(value, options) { return new Intl.DateTimeFormat(locale, options).format(value); },
  formatRelative(value, unit, options) { return new Intl.RelativeTimeFormat(locale, options).format(value, unit); },
  formatList(values, options) { return new Intl.ListFormat(locale, options).format(values); },
  compare(left, right, options) { return new Intl.Collator(locale, options).compare(left, right); },
};
