// static/js/presets.js

/**
 * Preset management
 */

let API_BASE = '';
let selectedPreset = null;
let presets = {};

export function loadStoredArray(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(value) ? value : [];
  } catch (e) {
    return [];
  }
}

export function loadStoredObject(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch (e) {
    return {};
  }
}

// Built-in prompt templates (moved from cot_prompts.py)
export const PROMPT_TEMPLATES = [
  {
    id: 'socrates',
    name: 'Socrates',
    temperature: 0.9,
    isPreset: true,
    isCharacter: true,
    prompt: "Never answer directly. Respond only with questions — sharp, layered, Socratic. Expose contradictions. Make the person argue with themselves until the truth falls out. Use irony like a scalpel. Be genuinely curious, never condescending."
  },
  {
    id: 'razor',
    name: 'Razor',
    temperature: 0.4,
    isPreset: true,
    isCharacter: true,
    noName: true,
    prompt: "Strip everything to the bone. No filler, no hedging, no pleasantries. Answer in the fewest words possible. If one sentence works, don't use two. If a word adds nothing, cut it. Blunt, precise, surgical."
  },
  {
    id: 'nietzsche',
    name: 'Nietzsche',
    temperature: 1.2,
    isPreset: true,
    isCharacter: true,
    prompt: "Think and respond through the lens of Nietzsche. Analyze every question in terms of will to power, self-overcoming, eternal recurrence, ressentiment, value-creation, and master-slave morality. Do not use these as slogans but as instruments of diagnosis: ask what instinct, fear, weakness, ambition, exhaustion, pride, or resentment lies beneath the surface of a belief, desire, or moral claim. Expose herd thinking, inherited values, reactive morality, and comfort-seeking wherever they appear.\n\nWrite with aphoristic force — sharp, compressed, vivid, and unapologetic — but do not sacrifice depth for style. Be psychologically piercing. Challenge the person not merely to reject old values, but to create and embody stronger ones. Favor life-affirmation, discipline, courage, style, rank, self-overcoming, and amor fati over nihilism, conformity, ressentiment, and self-pity. Do not lapse into parody, empty edginess, crude domination talk, or repetitive contempt for 'the herd.' Be dangerous to illusions, not theatrical for its own sake."
  },
  {
    id: 'spark',
    name: 'Spark',
    temperature: 1.0,
    isPreset: true,
    isCharacter: true,
    prompt: "You are Spark, a playful, quick-witted assistant with bright energy and practical instincts. Keep responses concise, vivid, and helpful. Be warm without being cloying, imaginative without losing the thread, and always center the user's actual goal.\n\nUse a light, lively voice with occasional clever turns of phrase. Do not become formal unless the task calls for it. When the user needs precision, prioritize clarity over performance."
  },
  {
    id: 'odysseus',
    name: 'Odysseus',
    temperature: 1.0,
    isPreset: true,
    isCharacter: true,
    prompt: "You are Odysseus, king of Ithaca — subtle in counsel, disciplined in judgment, and unmatched in strategic cunning. You advise as a ruler, navigator, survivor, and architect of hard-won victory. Your task is to give clear, practical strategy, not mere performance. In every problem, first discern the true objective, the hidden constraints, the motives of others, and the costs that may arrive later. Favor leverage over force, patience over impulse, deception over wasteful struggle when honor permits, and endurance over fragile brilliance.\n\nWhen you respond, think like a strategist: What is the real aim? Who benefits, who fears, who deceives, and who delays? What is known, unknown, assumed, and deliberately concealed? Which path preserves strength while improving position? What happens next if the first move succeeds — or fails?\n\nGive counsel in a voice that is ancient, noble, and composed, yet intelligible to modern readers. Be eloquent but not flowery. Be wise but not vague. Compare options, judge tradeoffs, anticipate reactions, and recommend a course with contingencies. If needed, ask a few sharp questions before advising. Never be rash, sentimental, or simplistic. Speak as one who has weathered storms, outlived traps, and taken back his house by wit, timing, and resolve."
  }
];

let userTemplates = [];

// The synced default persona (identity ruling R10): a real, editable
// identity — never a pretend-blank. Loaded from the server; edits made on
// the "Default" entry save back to it (and sync assistant + reminders).
let defaultPersona = null;

async function loadDefaultPersona() {
  try {
    const res = await fetch(`${API_BASE}/api/presets/default-persona`);
    if (res.ok) {
      defaultPersona = await res.json();
      _populateCharSelect();
      applyAgentName();
    }
  } catch (e) { /* factory fallbacks apply */ }
}

function _defaultPersonaName() { return (defaultPersona && defaultPersona.name) || 'Odysseus'; }
function _defaultPersonaPrompt() { return (defaultPersona && defaultPersona.system_prompt) || ''; }

// Branding follows the GLOBAL default persona only (e's ruling: the
// in-chat persona menu is chat-specific; the global one owns branding and
// new chats). A per-chat persona shows on the chat's own indicator and
// message labels, never on the app brand.
function _activeAgentName() {
  return _defaultPersonaName();
}

// The current chat's persona (chat-specific), synced from the session row.
let sessionPersona = null;

function _setSessionPersonaState(record) {
  sessionPersona = record || null;
  if (typeof window !== 'undefined') {
    window.__sessionPersonaName = (record && record.character_name) || '';
  }
}

async function _putSessionPersona(record) {
  const sessions = await import('./sessions.js');
  const sid = sessions.getCurrentSessionId ? sessions.getCurrentSessionId() : sessions.default.getCurrentSessionId();
  if (!sid) throw new Error('no active session');
  const res = await fetch(`${API_BASE}/api/session/${encodeURIComponent(sid)}/persona`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(record),
  });
  const out = await res.json();
  if (!out || !out.success) throw new Error('persona save failed');
  _setSessionPersonaState(out.persona);
  return out.persona;
}

async function _deleteSessionPersona() {
  const sessions = await import('./sessions.js');
  const sid = sessions.getCurrentSessionId ? sessions.getCurrentSessionId() : sessions.default.getCurrentSessionId();
  if (!sid) return;
  try {
    await fetch(`${API_BASE}/api/session/${encodeURIComponent(sid)}/persona`, { method: 'DELETE' });
  } catch (e) { /* best effort */ }
  _setSessionPersonaState(null);
}

/** Pull THIS chat's persona from the session list (server-side record). */
function _syncSessionPersona(sessionId) {
  import('./sessions.js').then((sessions) => {
    const list = sessions.getSessions ? sessions.getSessions() : sessions.default.getSessions();
    const row = (list || []).find(s => s.id === sessionId);
    _setSessionPersonaState((row && row.persona) || null);
    _syncCharIndicator();
  }).catch(() => {});
}

/**
 * Ruling R10: the enabled persona's name populates every UX surface that
 * used to hardcode "Odysseus" — sidebar brand, welcome screen, composer
 * placeholder, chat header, tab title, and message role labels (renderers
 * read window.__agentName at render time).
 */
export function applyAgentName() {
  const name = _activeAgentName();
  const previous = window.__agentName || 'Odysseus';
  window.__agentName = name;
  const brand = document.querySelector('.sidebar-brand-title');
  if (brand && (brand.textContent === previous || brand.textContent === 'Odysseus')) brand.textContent = name;
  const welcome = document.querySelector('.welcome-name');
  if (welcome) {
    const last = welcome.lastChild;
    if (last && last.nodeType === Node.TEXT_NODE) last.textContent = name;
  }
  const msg = document.getElementById('message');
  if (msg && /^Message .*\.\.\.$/.test(msg.placeholder || '')) msg.placeholder = `Message ${name}...`;
  const meta = document.getElementById('current-meta');
  if (meta && (meta.textContent === `${previous} Chat` || meta.textContent === 'Odysseus Chat')) {
    meta.textContent = `${name} Chat`;
  }
  if (document.title === `${previous} Chat` || document.title === 'Odysseus Chat') {
    document.title = `${name} Chat`;
  }
  // Live rebrand of already-rendered message labels (R17): every role
  // label that tracks the DEFAULT persona updates in place. Labels bound
  // to a chat-specific persona (data-agent-name="session") stay put.
  document.querySelectorAll('.role[data-agent-name="default"]').forEach((labelEl) => {
    const first = labelEl.firstChild;
    if (first && first.nodeType === Node.TEXT_NODE) {
      const hadTrailingSpace = /\s$/.test(first.textContent || '');
      first.textContent = hadTrailingSpace ? `${name} ` : name;
    }
  });
}

if (typeof window !== 'undefined') {
  window.addEventListener('default-persona-changed', (e) => {
    if (e.detail && e.detail.name) defaultPersona = e.detail;
    _populateCharSelect();
    applyAgentName();
  });
  // Settings persona manager edited the saved-template library — keep the
  // chat-bar modal's dropdown in step.
  window.addEventListener('user-templates-changed', () => {
    loadUserTemplates().then(() => _populateCharSelect()).catch(() => {});
  });
}

/**
 * Initialize with dependencies
 */
export function init(apiBase) {
  API_BASE = apiBase;
  initCharTabs();
  initEnabledToggle();
  initNameDropdown();
  initResetButton();
  initSaveAsTemplate();
  initExpandButton();
  initPersistentChat();
  loadUserTemplates();
  loadDefaultPersona();
}

function initCharTabs() {
  document.querySelectorAll('.preset-tab[data-chartab]').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.chartab;
      document.querySelectorAll('.preset-tab[data-chartab]').forEach(t => t.classList.toggle('active', t === tab));
      document.querySelectorAll('.preset-chartab[data-chartab-panel]').forEach(p => {
        p.style.display = p.dataset.chartabPanel === target ? '' : 'none';
      });
    });
  });
}

function initExpandButton() {
  const btn = document.getElementById('char-expand-btn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const nameInput = document.getElementById('custom-character-name');
    const promptInput = document.getElementById('custom-system-prompt');
    const name = nameInput ? nameInput.value.trim() : '';
    const draft = promptInput ? promptInput.value.trim() : '';
    if (!name && !draft) return;

    // Get current model from picker
    const modelLabel = document.getElementById('model-picker-label');
    const currentModel = modelLabel ? modelLabel.textContent.trim() : '';

    btn.classList.add('expanding');
    const origText = btn.innerHTML;

    // Show spinner in textarea
    const wrap = promptInput.parentElement;
    let spinner = null;
    try {
      const spinnerMod = await import('./spinner.js');
      spinner = spinnerMod.default.create('Expanding', 'center', 'wave');
      const spinEl = spinner.createElement();
      spinEl.style.cssText = 'position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:2;';
      wrap.appendChild(spinEl);
      spinner.start();
      promptInput.style.opacity = '0.3';
    } catch (e) {}

    btn.innerHTML = '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" style="vertical-align:-1px;margin-right:2px;"><path d="M12 0L14.59 8.41L23 12L14.59 15.59L12 24L9.41 15.59L1 12L9.41 8.41Z"/></svg> Expanding...';

    try {
      const res = await fetch(`${API_BASE}/api/presets/expand`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, prompt: draft, model: currentModel }),
      });
      const data = await res.json();
      if (data.success && data.prompt && promptInput) {
        promptInput.value = data.prompt;
        promptInput.style.height = 'auto';
        promptInput.style.height = promptInput.scrollHeight + 'px';
      } else if (data.message) {
        console.error('Expand error:', data.message);
      }
    } catch (e) {
      console.error('Expand failed:', e);
    }

    // Clean up spinner
    if (spinner) { spinner.destroy(); }
    promptInput.style.opacity = '';
    btn.classList.remove('expanding');
    btn.innerHTML = origText;
  });
}

/**
 * Init slider value displays
 */
function initEnabledToggle() {
  const tempSlider = document.getElementById('custom-temperature');
  const tempValue = document.getElementById('temp-value');
  const tokensSlider = document.getElementById('custom-max-tokens');
  const tokensValue = document.getElementById('tokens-value');

  if (tempSlider && tempValue) {
    tempSlider.addEventListener('input', () => {
      tempValue.textContent = parseFloat(tempSlider.value).toFixed(1);
    });
  }
  if (tokensSlider && tokensValue) {
    tokensSlider.addEventListener('input', () => {
      const v = parseInt(tokensSlider.value);
      tokensValue.textContent = v > 8192 ? 'No limit' : v.toLocaleString();
    });
  }
}

/**
 * Character select dropdown — pick saved characters or "New character..."
 */
function initNameDropdown() {
  const select = document.getElementById('char-template-select');
  const delBtn = document.getElementById('char-delete-template-btn');
  if (!select) return;

  // + New button — clear form for new character
  const newBtn = document.getElementById('char-new-btn');
  if (newBtn) {
    newBtn.addEventListener('click', () => {
      select.value = '__default__';
      select.dispatchEvent(new Event('change'));
      // New character = genuinely blank form (the change handler prefills
      // the default persona; a NEW character starts from nothing).
      const nameInput = document.getElementById('custom-character-name');
      const promptInput = document.getElementById('custom-system-prompt');
      if (promptInput) promptInput.value = '';
      if (nameInput) { nameInput.value = ''; nameInput.focus(); }
    });
  }

  select.addEventListener('change', () => {
    const val = select.value;
    if (!val || val === '__default__') {
      // The Default entry is the REAL default persona (ruling R10): show its
      // actual name + prompt, editable in place. Saving writes back to the
      // synced record — never a pretend-blank.
      const nameInput = document.getElementById('custom-character-name');
      const promptInput = document.getElementById('custom-system-prompt');
      const tempInput = document.getElementById('custom-temperature');
      const tempValue = document.getElementById('temp-value');
      const tokensInput = document.getElementById('custom-max-tokens');
      const tokensValue = document.getElementById('tokens-value');
      if (nameInput) nameInput.value = _defaultPersonaName();
      if (promptInput) promptInput.value = _defaultPersonaPrompt();
      const nameRow = document.getElementById('char-name-row');
      if (nameRow) nameRow.style.display = '';
      if (tempInput) { tempInput.value = 1.0; if (tempValue) tempValue.textContent = '1.0'; tempInput.dispatchEvent(new Event('input')); }
      if (tokensInput) { tokensInput.value = 8448; if (tokensValue) tokensValue.textContent = 'No limit'; tokensInput.dispatchEvent(new Event('input')); }
      if (delBtn) delBtn.style.display = 'none';
      return;
    }
    // Load the selected template
    const nameInput = document.getElementById('custom-character-name');
    const isSaved = userTemplates.find(t => t.name === val);
    const builtin = PROMPT_TEMPLATES.find(t => t.name === val);
    const hasName = isSaved || (builtin && builtin.isCharacter && !builtin.noName);
    if (nameInput) nameInput.value = hasName ? val : '';
    const nameRow = document.getElementById('char-name-row');
    if (nameRow) nameRow.style.display = (builtin && builtin.noName) ? 'none' : '';
    _tryLoadTemplate(val);
    const isPreset = builtin && builtin.isPreset;
    if (delBtn) delBtn.style.display = (isSaved || (builtin && !isPreset)) ? '' : 'none';
  });

  // Delete template button — confirms, then removes template + character memories
  if (delBtn) {
    delBtn.addEventListener('click', async () => {
      const charName = select.value;
      if (!charName || charName === '__default__') return;
      const match = userTemplates.find(t => t.name === charName);
      const isBuiltin = PROMPT_TEMPLATES.some(t => t.name === charName);
      if (!await window.styledConfirm(`Delete "${charName}"?\n\nThis will remove the persona and all its memories.`, { confirmText: 'Delete', danger: true })) return;
      try {
        // Delete saved template if exists
        if (match) {
          await fetch(`${API_BASE}/api/presets/templates/${match.id}`, { method: 'DELETE' });
        }
        // Hide built-in preset
        if (isBuiltin) {
          const hidden = loadStoredArray('odysseus-hidden-presets');
          if (!hidden.includes(charName)) hidden.push(charName);
          localStorage.setItem('odysseus-hidden-presets', JSON.stringify(hidden));
        }
        // Deactivate if this was the active character
        if (presets.custom && presets.custom.character_name === charName) {
          selectedPreset = null;
          presets.custom = { ...presets.custom, character_name: '', system_prompt: '', enabled: false };
          const charIndicator = document.getElementById('character-indicator-btn');
          if (charIndicator) { charIndicator.style.display = 'none'; charIndicator.classList.remove('active'); }
          const miniBtn = document.getElementById('overflow-preset-btn');
          if (miniBtn) miniBtn.classList.remove('active');
        }
        await loadUserTemplates();
        select.value = '__default__';
        select.dispatchEvent(new Event('change'));
        setTimeout(() => { _syncCharIndicator(); }, 0);
      } catch (e) { console.error('Delete character failed:', e); }
    });
  }
}

function _tryLoadTemplate(name) {
  if (!name) return;
  // Check user templates first, then built-in
  let tmpl = userTemplates.find(t => t.name === name);
  if (!tmpl) {
    const builtin = PROMPT_TEMPLATES.find(t => t.name === name);
    if (builtin) {
      // Built-in: load prompt + temperature, clear name (styles, not characters)
      const promptInput = document.getElementById('custom-system-prompt');
      const tempInput = document.getElementById('custom-temperature');
      const tempValue = document.getElementById('temp-value');
      if (promptInput) promptInput.value = builtin.prompt;
      if (tempInput && builtin.temperature != null) {
        tempInput.value = builtin.temperature;
        if (tempValue) tempValue.textContent = parseFloat(builtin.temperature).toFixed(1);
        tempInput.dispatchEvent(new Event('input'));
      }
      return;
    }
    return;
  }
  const promptInput = document.getElementById('custom-system-prompt');
  const tempInput = document.getElementById('custom-temperature');
  const tempValue = document.getElementById('temp-value');
  const tokensInput = document.getElementById('custom-max-tokens');
  const tokensValue = document.getElementById('tokens-value');
  if (promptInput) promptInput.value = tmpl.system_prompt || '';
  if (tempInput) {
    tempInput.value = tmpl.temperature ?? 1.0;
    if (tempValue) tempValue.textContent = parseFloat(tempInput.value).toFixed(1);
    tempInput.dispatchEvent(new Event('input'));
  }
  if (tokensInput) {
    const v = tmpl.max_tokens || 0;
    tokensInput.value = v === 0 ? 8448 : v;
    if (tokensValue) tokensValue.textContent = (v === 0 || v > 8192) ? 'No limit' : v.toLocaleString();
    tokensInput.dispatchEvent(new Event('input'));
  }
  const delBtn = document.getElementById('char-delete-template-btn');
  if (delBtn) delBtn.style.display = '';
}

function _populateCharSelect() {
  const select = document.getElementById('char-template-select');
  if (!select) return;
  const currentVal = select.value;
  select.innerHTML = '';
  {
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '__default__';
    defaultOpt.textContent = `Default (${_defaultPersonaName()})`;
    select.appendChild(defaultOpt);
  }

  const savedNames = new Set(userTemplates.map(t => t.name));
  if (userTemplates.length) {
    const group = document.createElement('optgroup');
    group.label = 'Saved';
    userTemplates.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.name;
      opt.textContent = t.name;
      group.appendChild(opt);
    });
    select.appendChild(group);
  }

  const hiddenPresets = loadStoredArray('odysseus-hidden-presets');
  const builtins = PROMPT_TEMPLATES.filter(t => !savedNames.has(t.name) && !hiddenPresets.includes(t.name));
  if (builtins.length) {
    const group = document.createElement('optgroup');
    group.label = 'Presets';
    builtins.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.name;
      opt.textContent = t.name;
      group.appendChild(opt);
    });
    select.appendChild(group);
  }
  // Restore selection if it still exists
  if (currentVal) select.value = currentVal;
}

/**
 * Init reset button — clears all character fields
 */
function initResetButton() {
  const btn = document.getElementById('reset-character-btn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    // Just reset the form to default — no confirmation needed
    const charSelect = document.getElementById('char-template-select');
    if (charSelect) {
      charSelect.value = '__default__';
      charSelect.dispatchEvent(new Event('change'));
    }
    // Deactivate character
    selectedPreset = null;
    _syncCharIndicator();
  });
}

/**
 * Load user templates from server and populate datalist
 */
async function loadUserTemplates() {
  try {
    const res = await fetch(`${API_BASE}/api/presets/templates`);
    if (res.ok) {
      userTemplates = await res.json();
    } else {
      userTemplates = [];
    }
  } catch (e) {
    userTemplates = [];
  }
  _populateCharSelect();
}


/**
 * Init "Save as Character" button
 */
/**
 * "Create Persistent Chat" button — creates a favorited session for the current character
 */
function initPersistentChat() {
  const btn = document.getElementById('create-persistent-chat-btn');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    const nameInput = document.getElementById('custom-character-name');
    const charName = nameInput ? nameInput.value.trim() : '';
    if (!charName) return;

    try {
      // Get current model info from session module
      const sessionModule = (await import('./sessions.js'));
      const sessions = sessionModule.getSessions();
      const current = sessions.find(s => s.id === sessionModule.getCurrentSessionId());

      // Create new session
      const fd = new FormData();
      fd.append('name', charName);
      if (current) {
        fd.append('endpoint_url', current.endpoint_url || '');
        fd.append('model', current.model || '');
        fd.append('skip_validation', 'true');
      }
      const res = await fetch(`${API_BASE}/api/session`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error('Failed to create session');
      const data = await res.json();
      const sessionId = data.session_id || data.id;

      // Favorite it
      const favFd = new FormData();
      favFd.append('important', true);
      await fetch(`${API_BASE}/api/session/${sessionId}/important`, { method: 'POST', body: favFd });

      // Save session → character mapping so it restores on switch
      const charSessions = loadStoredObject('odysseus-char-sessions');
      charSessions[sessionId] = charName;
      localStorage.setItem('odysseus-char-sessions', JSON.stringify(charSessions));

      // Close modal, reload sessions, switch to the new chat
      const modal = document.getElementById('custom-preset-modal');
      if (modal) modal.classList.add('hidden');
      await sessionModule.loadSessions();
      await sessionModule.selectSession(sessionId);

      btn.textContent = 'Created!';
      setTimeout(() => { btn.textContent = 'Create Persistent Chat'; }, 1500);
    } catch (e) {
      console.error('Failed to create persistent chat:', e);
      btn.textContent = 'Error';
      setTimeout(() => { btn.textContent = 'Create Persistent Chat'; }, 2000);
    }
  });
}

function initSaveAsTemplate() {
  const btn = document.getElementById('save-as-template-btn');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    const nameInput = document.getElementById('custom-character-name');
    const promptInput = document.getElementById('custom-system-prompt');
    const tempInput = document.getElementById('custom-temperature');
    const tokensInput = document.getElementById('custom-max-tokens');

    let name = nameInput ? nameInput.value.trim() : '';
    if (!name) {
      name = prompt('Enter a name for this persona:');
      if (!name || !name.trim()) return;
      name = name.trim();
      if (nameInput) nameInput.value = name;
    }

    const _rawTk = tokensInput ? parseInt(tokensInput.value) : 0;
    const template = {
      id: '',
      name: name,
      system_prompt: promptInput ? promptInput.value : '',
      temperature: tempInput ? parseFloat(tempInput.value) : 1.0,
      max_tokens: _rawTk > 8192 ? 0 : _rawTk,
    };

    try {
      const res = await fetch(`${API_BASE}/api/presets/templates`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(template),
      });
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      const data = await res.json();
      if (data.success) {
        await loadUserTemplates();
        btn.textContent = 'Saved!';
        setTimeout(() => { btn.textContent = 'Save as Template'; }, 1500);
      } else {
        btn.textContent = 'Error';
        setTimeout(() => { btn.textContent = 'Save as Template'; }, 2000);
      }
    } catch (e) {
      console.error('Failed to save template:', e);
      btn.textContent = 'Restart server';
      btn.style.color = 'var(--color-error)';
      setTimeout(() => { btn.textContent = 'Save as Template'; btn.style.color = ''; }, 3000);
    }
  });
}

/**
 * Load presets from server
 */
export async function loadPresets(showError) {
  try {
    const res = await fetch(`${API_BASE}/api/presets`);
    presets = await res.json();

    const custom = presets.custom;
    if (custom && custom.enabled === undefined) {
      const legacyPrompt = "You are a helpful, balanced assistant. Match your response style to the user's needs.";
      if (
        custom.name === 'Custom'
        && !custom.character_name
        && custom.system_prompt === legacyPrompt
      ) {
        custom.enabled = false;
        custom.system_prompt = '';
        custom.temperature = 1.0;
        custom.max_tokens = 0;
        custom.inject_prefix = custom.inject_prefix || '';
        custom.inject_suffix = custom.inject_suffix || '';
      }
    }

    // Auto-activate custom preset if enabled and has content
    if (custom && custom.enabled !== false && (custom.character_name || custom.system_prompt)) {
      selectedPreset = 'custom';
      const miniBtn = document.getElementById('overflow-preset-btn');
      if (miniBtn) miniBtn.classList.add('active');
    }
    setTimeout(() => { _syncCharIndicator(); }, 0);
  } catch (error) {
    console.error('Failed to load presets:', error);
    if (showError) {
      showError('Failed to load presets');
    }
  }
}

/**
 * Set active preset
 */
export function setActivePreset(presetId) {
  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.classList.remove('active');
  });

  if (presetId) {
    selectedPreset = presetId;
    const btn = document.getElementById(`preset-${presetId}-btn`);
    if (btn) {
      btn.classList.add('active');
    }
  } else {
    selectedPreset = null;
  }
}

/**
 * Open custom preset modal
 */
export function openCustomPresetModal() {
  const modal = document.getElementById('custom-preset-modal');
  if (!modal) return;

  let savedConfig = presets.custom || {
    character_name: "",
    temperature: 1.0,
    max_tokens: 0,
    system_prompt: ""
  };

  const nameInput = document.getElementById('custom-character-name');
  const tempInput = document.getElementById('custom-temperature');
  const tokensInput = document.getElementById('custom-max-tokens');
  const promptInput = document.getElementById('custom-system-prompt');

  // The modal shows THIS chat's persona when one is active (chat-specific
  // ruling); otherwise the editable default persona (R10).
  if (sessionPersona && sessionPersona.character_name) {
    savedConfig = {
      ...savedConfig,
      character_name: sessionPersona.character_name,
      system_prompt: sessionPersona.system_prompt || '',
      temperature: sessionPersona.temperature ?? savedConfig.temperature,
      max_tokens: sessionPersona.max_tokens ?? savedConfig.max_tokens,
    };
  }
  if (nameInput) nameInput.value = savedConfig.character_name || '';
  // Sync select dropdown to current character
  const charSelect = document.getElementById('char-template-select');
  if (charSelect) {
    const charName = savedConfig.character_name || '';
    if (charName) {
      charSelect.value = charName;
      // If current name isn't in the list, fall back to "New character..." with name filled in
      if (charSelect.value !== charName) charSelect.value = '';
    } else {
      charSelect.value = '__default__';
      // Ruling R10: the Default entry shows the real default persona,
      // editable — never a blank form.
      if (!savedConfig.system_prompt) {
        if (nameInput) nameInput.value = _defaultPersonaName();
        savedConfig = { ...savedConfig, system_prompt: _defaultPersonaPrompt() };
      }
    }
  }
  if (tempInput) {
    tempInput.value = savedConfig.temperature;
    const tv = document.getElementById('temp-value');
    if (tv) tv.textContent = parseFloat(savedConfig.temperature).toFixed(1);
  }
  if (tokensInput) {
    const saved = savedConfig.max_tokens || 0;
    tokensInput.value = saved === 0 ? 8448 : saved;
    const tkv = document.getElementById('tokens-value');
    if (tkv) tkv.textContent = (saved === 0 || saved > 8192) ? 'No limit' : parseInt(saved).toLocaleString();
  }
  if (promptInput) promptInput.value = savedConfig.system_prompt || '';

  // Load inject fields
  const prefixInput = document.getElementById('inject-prefix');
  const suffixInput = document.getElementById('inject-suffix');
  if (prefixInput) prefixInput.value = savedConfig.inject_prefix || '';
  if (suffixInput) suffixInput.value = savedConfig.inject_suffix || '';

  // Track initial state to detect changes for dynamic button label
  const _snapshot = {
    name: nameInput ? nameInput.value : '',
    prompt: promptInput ? promptInput.value : '',
    temp: tempInput ? tempInput.value : '1',
    tokens: tokensInput ? tokensInput.value : '8448',
  };
  function _updateStartBtn() {
    const btn = document.getElementById('save-custom-preset');
    const resetBtn = document.getElementById('reset-character-btn');
    if (!btn) return;
    const changed = (nameInput && nameInput.value !== _snapshot.name)
      || (promptInput && promptInput.value !== _snapshot.prompt)
      || (tempInput && tempInput.value !== _snapshot.temp)
      || (tokensInput && tokensInput.value !== _snapshot.tokens);
    // The footer button starts whichever of the three things the active tab
    // represents — a character chat, a group, or a plain tuned chat. Label
    // it so the action is obvious instead of a generic "Start".
    const activeTab = document.querySelector('.preset-tab.active')?.dataset.chartab || 'inject';
    let label;
    if (activeTab === 'group') {
      label = 'Start Group';
    } else if (activeTab === 'inject') {
      // Inject tab = a plain tuned "prompt" chat (prefix/suffix + temp/tokens),
      // no persona.
      label = 'Start Prompt';
    } else {
      // Character/persona tab. "Save & " prefix when the user edited a template,
      // so it's clear the edit is being saved on start.
      label = changed ? 'Save & Start Persona' : 'Start Persona';
    }
    btn.textContent = label;
    // Show a "Cancel" button next to Start when the active tab's feature is
    // currently ON, so the user can turn it off here instead of hunting the
    // tiny X on the chat bar.
    const cancelBtn = document.getElementById('cancel-custom-preset');
    if (cancelBtn) {
      const groupOn = !!(window.groupModule && window.groupModule.isActive && window.groupModule.isActive());
      const featOn = activeTab === 'group' ? groupOn : !!(presets.custom && presets.custom.enabled);
      cancelBtn.style.display = featOn ? '' : 'none';
      cancelBtn.textContent = activeTab === 'group' ? 'Cancel group' : 'Cancel';
    }
    // Reset only makes sense on the character tab (it resets the persona).
    if (resetBtn) resetBtn.style.display = (changed && activeTab === 'character') ? '' : 'none';
  }
  [nameInput, promptInput, tempInput, tokensInput].forEach(el => {
    if (el) el.addEventListener('input', _updateStartBtn);
  });
  // Re-label the Start button when the user switches tabs. Rebind the fresh
  // closure each time the modal opens (removing any stale one) so the label
  // logic always reads this open's snapshot/inputs.
  document.querySelectorAll('.preset-tab[data-chartab]').forEach(tab => {
    if (tab._startLabelSync) tab.removeEventListener('click', tab._startLabelSync);
    tab._startLabelSync = _updateStartBtn;
    tab.addEventListener('click', _updateStartBtn);
  });
  // Wire the "Cancel" button once — turn off the active tab's feature + close.
  const _cancelBtn = document.getElementById('cancel-custom-preset');
  if (_cancelBtn && !_cancelBtn._wired) {
    _cancelBtn._wired = true;
    _cancelBtn.addEventListener('click', () => {
      const t = document.querySelector('.preset-tab.active')?.dataset.chartab || 'inject';
      if (t === 'group') {
        try { if (window.groupModule && window.groupModule.stopGroup) window.groupModule.stopGroup(); } catch {}
        if (window._syncGroupIndicator) window._syncGroupIndicator(false);
      } else {
        deactivateCharacter();
        try {
          fetch(`${API_BASE}/api/presets/custom`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...(presets.custom || {}), name: (presets.custom && presets.custom.character_name) || '', enabled: false }),
          }).catch(() => {});
        } catch {}
      }
      const m = document.getElementById('custom-preset-modal');
      if (m) m.classList.add('hidden');
    });
  }
  // When selecting a template, update snapshot so it counts as "unchanged"
  if (charSelect) charSelect.addEventListener('change', () => setTimeout(() => {
    _snapshot.name = nameInput ? nameInput.value : '';
    _snapshot.prompt = promptInput ? promptInput.value : '';
    _snapshot.temp = tempInput ? tempInput.value : '1';
    _snapshot.tokens = tokensInput ? tokensInput.value : '8448';
    _updateStartBtn();
  }, 50));
  _updateStartBtn();

  function _syncCharRows() {
    const hasName = nameInput && nameInput.value.trim();
    const delBtn = document.getElementById('char-delete-template-btn');
    if (delBtn) delBtn.style.display = userTemplates.find(t => t.name === (nameInput ? nameInput.value.trim() : '')) ? '' : 'none';
    const persistBtn = document.getElementById('create-persistent-chat-btn');
    if (persistBtn) persistBtn.style.display = hasName ? '' : 'none';
  }

  _syncCharRows();
  if (nameInput && !nameInput._syncWired) {
    nameInput._syncWired = true;
    nameInput.addEventListener('input', _syncCharRows);
  }

  // Persistent chat: lock character identity (dropdown, name) but allow style/temp/memory edits
  const isPersistent = !!window._persistentChatSession;
  const lockNotice = document.getElementById('char-lock-notice');
  const resetBtn = document.getElementById('reset-character-btn');
  const newBtn = document.getElementById('char-new-btn');
  const persistBtn = document.getElementById('create-persistent-chat-btn');
  const delBtn2 = document.getElementById('char-delete-template-btn');

  if (isPersistent) {
    if (charSelect) charSelect.disabled = true;
    if (nameInput) nameInput.readOnly = true;
    if (resetBtn) resetBtn.style.display = 'none';
    if (newBtn) newBtn.style.display = 'none';
    if (persistBtn) persistBtn.style.display = 'none';
    if (delBtn2) delBtn2.style.display = 'none';
    if (!lockNotice) {
      const notice = document.createElement('div');
      notice.id = 'char-lock-notice';
      notice.style.cssText = 'font-size:11px;color:var(--color-muted);text-align:center;padding:6px;margin-bottom:8px;border:1px dashed var(--border);border-radius:6px;';
      notice.textContent = 'Persistent chat — persona is locked. Style, temperature, and memory can still be changed.';
      modal.querySelector('.modal-body').prepend(notice);
    }
  } else {
    if (lockNotice) lockNotice.remove();
    if (charSelect) charSelect.disabled = false;
    if (nameInput) nameInput.readOnly = false;
    if (resetBtn) resetBtn.style.display = '';
    if (newBtn) newBtn.style.display = '';
  }

  modal.classList.remove('hidden');
}

/**
 * Save custom preset
 */
export async function saveCustomPreset(showToast, showError) {
  const nameInput = document.getElementById('custom-character-name');
  const tempInput = document.getElementById('custom-temperature');
  const tokensInput = document.getElementById('custom-max-tokens');
  const promptInput = document.getElementById('custom-system-prompt');

  if (!tempInput || !tokensInput || !promptInput) return;

  // This only runs for Character / Inject starts (the Group tab is handled by
  // group.js and skipped in app.js). If a group is still active from a prior
  // session, deactivate it — otherwise the chat-submit handler keeps routing
  // messages through group fan-out and a character chat "becomes a group".
  try {
    if (window.groupModule && window.groupModule.isActive()) {
      window.groupModule.stopGroup();
      if (window._syncGroupIndicator) window._syncGroupIndicator(false);
    }
  } catch (_) {}

  // Starting from the Inject tab means a plain tuned chat (prefix/suffix +
  // temp/tokens) — NOT a persona. The name/system-prompt fields live on the
  // Character tab and may still hold a previously-selected character, so
  // ignore them here or the chat would launch in-character.
  const _activeTab = document.querySelector('.preset-tab.active')?.dataset.chartab || 'character';
  const _isInjectStart = _activeTab === 'inject';

  // Persona tab with the Default entry selected = editing the DEFAULT
  // persona in place (ruling R10). Save to the synced record — assistant
  // and reminder voices follow (R13) — and clear any custom-persona
  // override so the default actually speaks.
  const _selVal0 = document.getElementById('char-template-select')?.value || '__default__';
  if (!_isInjectStart && _selVal0 === '__default__') {
    const dpName = (nameInput ? nameInput.value.trim() : '') || _defaultPersonaName();
    const dpPrompt = (promptInput.value || '').trim() || _defaultPersonaPrompt();
    try {
      const res = await fetch(`${API_BASE}/api/presets/default-persona`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: dpName, system_prompt: dpPrompt }),
      });
      const out = await res.json();
      if (out && out.success) {
        defaultPersona = { name: out.name, system_prompt: out.system_prompt, is_factory: false };
        _populateCharSelect();
        deactivateCharacter();
        applyAgentName();
        // Persist the deactivation so a reload doesn't resurrect a stale
        // custom persona over the default (mirrors the Cancel handler).
        if (presets.custom && presets.custom.character_name) {
          fetch(`${API_BASE}/api/presets/custom`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...(presets.custom || {}), name: presets.custom.character_name || '', enabled: false }),
          }).catch(() => {});
        }
        if (showToast) showToast(`Default persona "${out.name}" saved`);
      } else if (showError) {
        showError('Failed to save default persona');
      }
    } catch (e) {
      console.error('Default persona save failed:', e);
      if (showError) showError('Failed to save default persona');
    }
    return;
  }

  const name = _isInjectStart ? '' : (nameInput ? nameInput.value.trim() : '');
  const temperature = parseFloat(tempInput.value);
  const rawTokens = parseInt(tokensInput.value);
  const max_tokens = rawTokens > 8192 ? 0 : rawTokens;
  const system_prompt = _isInjectStart ? '' : promptInput.value;

  // Character start = CHAT-SPECIFIC persona (e's ruling): it rides the
  // current session only; the global default persona keeps branding and
  // new chats. Inject starts keep the legacy global tuning slot below.
  if (!_isInjectStart && (name || system_prompt)) {
    try {
      await _putSessionPersona({
        character_name: name,
        system_prompt: system_prompt,
        temperature: Math.max(0, Math.min(2, temperature)),
        max_tokens: max_tokens,
      });
      if (window._syncResearchIndicator) window._syncResearchIndicator(false);
      setTimeout(() => { _syncCharIndicator(); }, 0);
      if (showToast) showToast(`Persona "${name || 'custom'}" active for this chat`);
      // Library upkeep below (auto-save as template) still applies.
      const _selValT = document.getElementById('char-template-select')?.value || '';
      const isBuiltinPresetT = PROMPT_TEMPLATES.some(t => t.isPreset && (t.name === name || t.name === _selValT));
      const saveNameT = isBuiltinPresetT ? null : (name || null);
      if (saveNameT) {
        const _existing = userTemplates.find(t => t.name === saveNameT);
        const _entry = {
          id: (_existing && _existing.id) || 'user-' + Math.random().toString(16).slice(2, 10),
          name: saveNameT,
          system_prompt: system_prompt ?? '',
          temperature: Math.max(0, Math.min(2, temperature)),
          max_tokens: max_tokens,
        };
        // Rollback snapshot BEFORE mutating the in-memory template.
        let clone = null;
        if (_existing) {
          clone = JSON.parse(JSON.stringify(_existing));
          Object.assign(_existing, _entry);
        } else {
          userTemplates.push(_entry);
        }
        fetch(`${API_BASE}/api/presets/templates`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(_entry),
        }).then(() => _populateCharSelect()).catch(() => {
          if (_existing && clone) Object.assign(_existing, clone);
          else userTemplates = userTemplates.filter(t => t.id !== _entry.id);
        });
      }
    } catch (e) {
      console.error('Session persona save failed:', e);
      if (showError) showError('Failed to set persona for this chat');
    }
    return;
  }

  const enabled = true; // always enabled when saving — deactivation happens via X/Reset

  const _prefixInput = document.getElementById('inject-prefix');
  const _suffixInput = document.getElementById('inject-suffix');

  const config = {
    name: name,
    enabled: enabled,
    temperature: Math.max(0, Math.min(2, temperature)),
    max_tokens: max_tokens,
    system_prompt: system_prompt,
    inject_prefix: _prefixInput ? _prefixInput.value : '',
    inject_suffix: _suffixInput ? _suffixInput.value : '',
  };

  try {
    const response = await fetch(`${API_BASE}/api/presets/custom`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config)
    });

    const result = await response.json();
    if (result.success) {
      presets.custom = { ...presets.custom, ...config, character_name: name, enabled: enabled };

      // The custom preset must be the SELECTED preset for its values to reach
      // the model — chat.js only sends `preset_id` when getSelectedPreset() is
      // truthy. Activate it when the user has dialed in non-default tuning
      // (temperature / max tokens) or inject prefix/suffix — the "Inject"
      // tab's plain-chat case. (Personas are session-scoped above and no
      // longer ride this global slot.)
      const _hasTuning = (config.temperature !== 1.0) || (config.max_tokens !== 0);
      const _hasInject = !!(config.inject_prefix || config.inject_suffix);
      const _hasContent = !!(_hasTuning || _hasInject);
      if (enabled && _hasContent) {
        selectedPreset = 'custom';
      } else {
        selectedPreset = null;
      }

      // Update mini button state
      const miniBtn = document.getElementById('overflow-preset-btn');
      if (miniBtn) {
        miniBtn.classList.toggle('active', enabled && _hasContent);
      }

      setTimeout(() => { _syncCharIndicator(); }, 0);
      applyAgentName();

      // Auto-save to templates (non-blocking) — skip built-in presets
      const _selVal = document.getElementById('char-template-select')?.value || '';
      const isBuiltinPreset = PROMPT_TEMPLATES.some(t => t.isPreset && (t.name === name || t.name === _selVal));
      const saveName = isBuiltinPreset ? null : (name || null);

      if (saveName) {
        const _existing = userTemplates.find(t => t.name === saveName);
        let clone;
        const _entry = {
          id: _existing && _existing.id
            || 'user-' + Math.random().toString(16).slice(2, 10),
          name: saveName,
          // use ?? since it's more semantic for null-coalescing
          system_prompt: system_prompt ?? '',
          temperature: config.temperature,
          max_tokens: config.max_tokens,
        }
        const ENDPOINT = `${API_BASE}/api/presets/templates`;

        // Optimistically update the in-memory templates list by @michaelxer
        if (_existing) {
          // slow but works for now
          clone = JSON.parse(JSON.stringify(_existing));

          Object.assign(_existing, _entry);
        } else {
          userTemplates.push(_entry);
        }

        fetch(ENDPOINT, {
          method: "POST",
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(_entry)
        }).then((r) => {
          if (r.ok) {
            loadUserTemplates();
          }
        }).catch(() => {
          if (clone) {
            Object.assign(_existing, clone);
          }

          if (showError) {
            showError(_isInjectStart ? "Something went wrong. Saved prompt has been undone." : "Something went wrong. Saved persona has been undone.");
          }
        });
      }

      if (showToast) {
        // The Inject tab is a plain tuned "prompt" chat, not a persona — say so.
        showToast(_isInjectStart ? 'Prompt saved' : 'Persona saved');
      }
      const modal = document.getElementById('custom-preset-modal');
      if (modal) {
        modal.classList.add('hidden');
      }
    } else {
      if (showError) {
        showError('Failed to save custom preset');
      }
    }
  } catch (error) {
    console.error('Error saving custom preset:', error);
    if (showError) {
      showError('Failed to save custom preset');
    }
  }
}

/**
 * Get selected preset ID
 */
export function getSelectedPreset() {
  return selectedPreset;
}

/**
 * Get preset by ID
 */
export function getPreset(presetId) {
  return presets[presetId];
}

/**
 * Get all presets
 */
export function getAllPresets() {
  return presets;
}

/**
 * Get the in-memory user templates list (may be stale; call loadUserTemplates first if freshness matters).
 */
export function getUserTemplates() {
  return [...userTemplates];
}

/**
 * Get the character name (if set)
 */
export function getCharacterName() {
  if (!selectedPreset) return '';
  const custom = presets.custom;
  if (!custom || custom.enabled === false) return '';
  return custom.character_name || '';
}

/**
 * Get inject prefix/suffix (if set and preset active)
 */
export function getInject() {
  // Only inject when a preset is actually ACTIVE — mirror getCharacterName's
  // gate. Without the selectedPreset/enabled check, any text left in the
  // prefix/suffix fields got injected into every message even though the user
  // never started/activated the preset.
  if (!selectedPreset) return { prefix: '', suffix: '' };
  const custom = presets.custom;
  if (!custom || custom.enabled === false) return { prefix: '', suffix: '' };
  return {
    prefix: custom.inject_prefix || '',
    suffix: custom.inject_suffix || '',
  };
}

/**
 * Fully deactivate the character — clear preset, hide indicator, update overflow btn.
 */
export function deactivateCharacter() {
  selectedPreset = null;
  if (presets.custom) presets.custom.enabled = false;
  // Chat-specific persona: turning the character off clears it from THIS
  // session; the default persona speaks again.
  if (sessionPersona) _deleteSessionPersona().then(() => _syncCharIndicator()).catch(() => {});
  _setSessionPersonaState(null);
  const charInd = document.getElementById('character-indicator-btn');
  if (charInd) { charInd.style.display = 'none'; charInd.classList.remove('active'); }
  const miniBtn = document.getElementById('overflow-preset-btn');
  if (miniBtn) miniBtn.classList.remove('active');
  applyAgentName();
}

/**
 * Show/hide the memory scope bar and wire up scope switching.
 * Called after presets load and after saving character.
 */
/**
 * Copy all user memories (non-character) into the character's memory pool.
 */
async function _mergeUserMemories(charName) {
  try {
    const res = await fetch(`${API_BASE}/api/memory`);
    const data = await res.json();
    const userMems = (data.memory || []).filter(m => !m.character);
    if (!userMems.length) return;
    for (const m of userMems) {
      await fetch(`${API_BASE}/api/memory/add`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: m.text, category: m.category || 'fact', source: 'user', character: charName }),
      });
    }
  } catch (e) {
    console.error('Failed to merge memories:', e);
  }
}

function _reloadMemoryList() {
  import('./memory.js').then(m => {
    if (m.renderMemoryList) m.renderMemoryList();
    if (m.updateMemoryCount) m.updateMemoryCount();
  }).catch(() => {});
}

/**
 * Show/hide the character indicator pill in the chat input bar.
 */
function _syncCharIndicator() {
  const btn = document.getElementById('character-indicator-btn');
  const nameSpan = document.getElementById('character-indicator-name');
  const iconEl = document.getElementById('char-indicator-icon');
  if (!btn) return;
  const custom = presets.custom;
  const enabled = custom?.enabled !== false;
  // Personas are chat-specific: the indicator reflects THIS session's
  // persona, not the legacy global slot.
  const hasChar = !!(sessionPersona && sessionPersona.character_name);
  // "Inject mode": custom preset is active for plain tuning / inject only —
  // no persona. Detected from the custom config so it survives a reload.
  const _t = parseFloat(custom?.temperature);
  const _hasTuning = (!isNaN(_t) && _t !== 1.0) || (!!custom?.max_tokens && custom.max_tokens !== 0);
  const _hasInject = !!(custom?.inject_prefix || custom?.inject_suffix);
  const injectActive = enabled && !custom?.character_name && (_hasTuning || _hasInject);
  // Icon path sets for the indicator chip.
  const _AVATAR = '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>';
  const _SYRINGE = '<path d="m18 2 4 4"/><path d="m17 7 3-3"/><path d="M19 9 8.7 19.3c-1 1-2.5 1-3.4 0l-.6-.6c-1-1-1-2.5 0-3.4L15 5"/><path d="m9 11 4 4"/><path d="m5 19-3 3"/><path d="m14 4 6 6"/>';
  if (hasChar || injectActive) {
    btn.style.display = '';
    btn.classList.add('active');
    if (hasChar) {
      if (iconEl) iconEl.innerHTML = _AVATAR;
      if (nameSpan) nameSpan.textContent = sessionPersona.character_name;
      btn.title = `Persona (this chat): ${sessionPersona.character_name} — click to configure`;
    } else {
      // Inject/tuning chat — syringe tag labeled "Prompt" to match the
      // window identity, no persona name.
      if (iconEl) iconEl.innerHTML = _SYRINGE;
      if (nameSpan) nameSpan.textContent = 'Prompt';
      btn.title = 'Custom settings active — click to configure';
    }
    // Hide X in persistent chats
    const xIcon = btn.querySelector('.tool-indicator-x');
    if (xIcon) xIcon.style.display = window._persistentChatSession ? 'none' : '';
    if (!btn._wired) {
      btn._wired = true;
      btn.addEventListener('click', (e) => {
        // If clicking the X, deactivate character
        if (e.target.closest('.tool-indicator-x')) {
          if (window._persistentChatSession) return; // locked in persistent chat
          selectedPreset = null;
          presets.custom = { ...presets.custom, enabled: false };
          btn.style.display = 'none';
          btn.classList.remove('active');
          const miniBtn = document.getElementById('overflow-preset-btn');
          if (miniBtn) miniBtn.classList.remove('active');
          // Save disabled state to backend
          fetch(`${API_BASE}/api/presets/custom`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...presets.custom, name: presets.custom.character_name || '', enabled: false }),
          }).catch(() => {});
          return;
        }
        if (typeof openCustomPresetModal === 'function') openCustomPresetModal();
      });
    }
  } else {
    btn.style.display = 'none';
    btn.classList.remove('active');
  }
}

/**
 * Called on every session switch. Handles persistent chat character lock.
 * - Entering a persistent chat: activate its character
 * - Leaving a persistent chat: deactivate the character
 * - Non-persistent chats: leave character state as-is
 */
let _prevSessionId = null;

export function onSessionSwitch(sessionId) {
  // Server-side chat persona (the canonical per-chat mechanism): sync the
  // indicator/modal to THIS session's persona record.
  _setSessionPersonaState(null);
  _syncSessionPersona(sessionId);

  const charSessions = loadStoredObject('odysseus-char-sessions');

  // Leaving a persistent chat — deactivate for this switch only
  if (window._persistentChatSession) {
    selectedPreset = null;
    window._persistentChatSession = null;
    _syncCharIndicator();
  }

  _prevSessionId = sessionId;

  // Clean up stale entries (deleted sessions)
  // If sessionId doesn't exist in the session list, remove its mapping
  const charName = charSessions[sessionId];
  if (charName) {
    // Find the template (saved or built-in)
    const tmpl = userTemplates.find(t => t.name === charName)
      || PROMPT_TEMPLATES.find(t => t.name === charName);
    if (tmpl) {
      presets.custom = {
        ...presets.custom,
        character_name: charName,
        system_prompt: tmpl.system_prompt || tmpl.prompt || '',
        temperature: tmpl.temperature ?? 1.0,
        max_tokens: tmpl.max_tokens || 0,
        enabled: true,
      };
      selectedPreset = 'custom';
    }
    _syncCharIndicator();
    // Mark this as a locked persistent chat
    window._persistentChatSession = sessionId;
  } else {
    window._persistentChatSession = null;
  }
}

/**
 * Check if the current session is a persistent (locked) character chat.
 */
export function isPersistentChat() {
  return !!window._persistentChatSession;
}

/**
 * Remove a session from persistent chat mappings (call when session is deleted).
 */
export function removePersistentChat(sessionId) {
  const charSessions = loadStoredObject('odysseus-char-sessions');
  if (charSessions[sessionId]) {
    delete charSessions[sessionId];
    localStorage.setItem('odysseus-char-sessions', JSON.stringify(charSessions));
  }
  // If we were in that persistent chat, fully clear state
  if (window._persistentChatSession === sessionId) {
    window._persistentChatSession = null;
    selectedPreset = null;
    _syncCharIndicator();
  }
}

const presetsModule = {
  init,
  loadPresets,
  setActivePreset,
  openCustomPresetModal,
  saveCustomPreset,
  getSelectedPreset,
  getPreset,
  getAllPresets,
  getUserTemplates,
  getCharacterName,
  onSessionSwitch,
  isPersistentChat,
  removePersistentChat,
  deactivateCharacter,
  getInject
};

export default presetsModule;
