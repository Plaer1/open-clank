#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7777').replace(/\/$/, '');
const port = await new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-models-'));
const browserExecutable = process.env.OPENCLANK_CHROME_BIN
  || process.env.CHROME_BIN
  || process.env.ODYSSEUS_BROWSER_EXECUTABLE
  || ['/usr/bin/chromium', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    .find(candidate => fs.existsSync(candidate));
assert(browserExecutable, 'Chromium or Google Chrome is required for this browser acceptance test');
const chromium = spawn(browserExecutable, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  let targets;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      targets = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json());
      break;
    } catch { await new Promise(resolve => setTimeout(resolve, 50)); }
  }
  const target = targets?.find(item => item.type === 'page');
  assert(target?.webSocketDebuggerUrl, 'Chromium page target is unavailable');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });

  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const request = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(request.timer);
    message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
  });
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => reject(new Error(`${method} timed out`)), 20_000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const response = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    return response.result.value;
  };
  const waitFor = async (expression, label) => {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      try { if (await evaluate(expression)) return; } catch {}
      await new Promise(resolve => setTimeout(resolve, 75));
    }
    throw new Error(`Timed out waiting for ${label}`);
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', { url: `${base}/login` });
  await waitFor("document.readyState === 'complete'", 'login origin');
  const addedModelsState = await evaluate(`(async () => {
    const html = await fetch('/static/index.html').then(response => response.text());
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    document.body.replaceChildren(parsed.getElementById('settings-modal'));
    const errors = [];
    window.addEventListener('error', event => errors.push(event.error?.message || event.message));
    window.addEventListener('unhandledrejection', event => errors.push(
      event.reason?.message || String(event.reason),
    ));
    window.modelsModule = { refreshModels: async () => {} };
    window.sessionModule = { updateModelPicker() {} };
    const requests = [];
    const json = data => new Response(JSON.stringify(data), {
      status:200, headers:{'Content-Type':'application/json'},
    });
    window.fetch = async (input, options = {}) => {
      const url = String(input);
      requests.push({ url, method:options.method || 'GET' });
      if (url.endsWith('/api/v1/providers/families')) {
        return json({ families:[
          {
            id:'openai', display_name:'OpenAI', adapters:['openai-responses'],
            kinds:['official'], billing_lanes:['metered_api'], model_count:1,
            auth_methods:[
              { id:'api_key', type:'api', label:'API key' },
              { id:'oauth:0', type:'oauth', label:'Browser login' },
            ],
          },
          {
            id:'ollama', display_name:'Ollama', adapters:['ollama'],
            kinds:['local'], billing_lanes:['local'], model_count:1,
            auth_methods:[{ id:'none', type:'none', label:'No API key' }],
          },
        ] });
      }
      if (url.endsWith('/api/v1/providers/connections')) {
        return json({ connections:[{
          id:'pcn-browser', family_id:'openai', adapter_id:'openai-responses',
          kind:'official', billing_lane:'metered_api', label:'OpenAI API',
          url:null, enabled:true, revision:1,
        }] });
      }
      if (url.endsWith('/api/v1/providers/connections/pcn-browser/accounts')) {
        return json({ accounts:[{
          id:'pac-browser', connection_id:'pcn-browser', label:'Primary',
          auth_method:'api_key', auth_class:'metered', order:0, enabled:true, revision:1,
        }] });
      }
      if (url.endsWith('/api/v1/providers/models')) {
        return json({ models:[{
          id:'pmr-browser', connection_id:'pcn-browser', model_id:'gpt-browser',
          display_name:'GPT Browser', operations:['chat.stream'], enabled:true, revision:1,
        }] });
      }
      if (url.endsWith('/api/v1/providers/bindings')) return json({ bindings:[] });
      if (url.endsWith('/api/v1/providers/shares')) return json({ shares:[] });
      if (url.endsWith('/api/v1/providers/shares/received')) return json({ shares:[] });
      return json({});
    };
    let selected = '';
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = function () {
      selected = this.dataset.providerConnectionId || '';
    };
    const providers = await import('/static/js/providerControl.js?added-models-regression=2');
    providers.init();
    await providers.load();
    providers.selectConnection('pcn-browser');
    HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    return {
      quickAdd: document.getElementById('provider-control-create')?.textContent.trim() || '',
      added: document.getElementById('provider-control-connections')?.textContent.trim() || '',
      addModes:[...document.querySelectorAll('[data-provider-add-mode]')].map(form => form.dataset.providerAddMode).sort(),
      nativeProviderSelects:document.querySelectorAll('[data-settings-panel="services"] select, [data-settings-panel="added-models"] select').length,
      familyPickers:[...document.querySelectorAll('[data-provider-add-mode] [role="combobox"]')].map(trigger => ({
        expanded:trigger.getAttribute('aria-expanded'),
        haspopup:trigger.getAttribute('aria-haspopup'),
        listbox:document.getElementById(trigger.getAttribute('aria-controls'))?.getAttribute('role'),
      })),
      connectionGroups:[...document.querySelectorAll('[data-provider-connection-group]')].map(group => group.dataset.providerConnectionGroup),
      expandButtons:[...document.querySelectorAll('.provider-control-connection-toggle')].map(button => ({
        expanded:button.getAttribute('aria-expanded'),
        controls:button.getAttribute('aria-controls'),
      })),
      addedTabHidden: document.querySelector('[data-settings-tab="added-models"]')?.classList.contains('hidden'),
      selected,
      requests,
      errors,
    };
  })()`);
  assert.match(addedModelsState.quickAdd, /Add API Models/, 'Add Models renders the compact API form');
  assert.match(addedModelsState.quickAdd, /Add Local Models/, 'Add Models renders the compact local form');
  assert.deepEqual(addedModelsState.addModes, ['local', 'remote']);
  assert.equal(addedModelsState.nativeProviderSelects, 0, 'provider settings render no native select elements');
  assert.equal(addedModelsState.familyPickers.length, 2);
  assert.ok(addedModelsState.familyPickers.every(picker => (
    picker.expanded === 'false' && picker.haspopup === 'listbox' && picker.listbox === 'listbox'
  )), 'provider family choices use collapsed ARIA combobox/listbox controls');
  assert.match(addedModelsState.added, /OpenAI API/, 'Added Models renders normalized connections');
  assert.match(addedModelsState.added, /GPT Browser/, 'Added Models renders normalized model routes');
  assert.match(addedModelsState.added, /Refresh models/, 'model discovery is a plain per-provider action');
  assert.doesNotMatch(addedModelsState.added, /chat\.stream|chat\.complete/,
    'normal model rows do not expose operation identifiers');
  assert.deepEqual(addedModelsState.connectionGroups, ['api'], 'Added Models groups API connection rows');
  assert.equal(addedModelsState.expandButtons.length, 1);
  assert.equal(addedModelsState.expandButtons[0].expanded, 'false', 'compact connection row begins collapsed');
  assert.ok(addedModelsState.expandButtons[0].controls, 'compact connection row identifies its managed details');
  assert.equal(addedModelsState.addedTabHidden, false, 'Added Models remains a visible settings destination');
  assert.equal(addedModelsState.selected, 'pcn-browser', 'connection selection reaches the sibling Added Models panel');
  assert.ok(addedModelsState.requests.some(request => request.url.includes('/api/v1/providers/connections')));
  assert.equal(addedModelsState.requests.some(request => (
    request.url.includes('/api/model-endpoints') || request.url.includes('/api/mimo/providers')
  )), false, 'provider views do not fall back to retired provider APIs');
  assert.equal(addedModelsState.requests.some(request => request.url.includes('/eligibility')), false,
    'Added Models first paint does not wait for per-model health');
  assert.deepEqual(addedModelsState.errors, [], 'Added Models renders without browser errors');

  await evaluate(`(async () => {
    document.body.innerHTML = '<main><div id="models"></div></main>';
    globalThis.__openClankAuthenticatedUser = 'browser-test';
    const favoriteKey = 'odysseus-model-favorites:scope:browser-test';
    localStorage.setItem(favoriteKey, JSON.stringify(['shared-model']));
    window.__catalogItems = [
      { endpoint_id:'endpoint-a', endpoint_name:'Same name', url:'https://a.invalid/v1/chat/completions', category:'api', models:['shared-model','only-a'] },
      { endpoint_id:'endpoint-b', endpoint_name:'Same name', url:'https://b.invalid/v1/chat/completions', category:'api', models:['shared-model','only-b'] },
    ];
    window.__pickerPatch = null;
    window.fetch = async (input, options = {}) => {
      const url = String(input);
      if (url.includes('/api/models')) {
        return new Response(JSON.stringify({ items: window.__catalogItems }), {
          status:200, headers:{'Content-Type':'application/json'},
        });
      }
      if (options.method === 'PATCH' && url.includes('/api/session/')) {
        const fields = Object.fromEntries(options.body.entries());
        window.__pickerPatch = fields;
        return new Response(JSON.stringify({
          id: 'session-1',
          model: fields.model,
          endpoint_url: fields.endpoint_url,
          endpoint_id: fields.endpoint_id,
        }), { status:200, headers:{'Content-Type':'application/json'} });
      }
      return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    };
    const models = await import('/static/js/models.js?catalog-identity=1');
    models.init('');
    await models.refreshModels(true);
    window.__catalogModels = models;
    window.modelsModule = models;
  })()`);
  await waitFor("document.querySelectorAll('.models-row').length === 4", 'four unique endpoint-model choices');

  // Chromium's CDP structured clone can stall on this DOM-derived object even
  // though each field is plain data. Serialize in-page, then parse in Node.
  const state = JSON.parse(await evaluate(`(() => JSON.stringify({
    rowKeys: [...document.querySelectorAll('.models-row')].map(row => row.dataset.modelId),
    mids: [...document.querySelectorAll('.models-row')].map(row => row.dataset.modelMid),
    endpointLabels: [...document.querySelectorAll('.models-endpoint-label span:nth-child(2)')].map(node => node.textContent),
    favoriteRows: document.querySelectorAll('.models-category-header + .models-group-content .models-row').length,
    storedFavorites: JSON.parse(localStorage.getItem('odysseus-model-favorites:scope:browser-test') || '[]'),
  }))()`));
  assert.equal(new Set(state.rowKeys).size, 4, 'one row per endpoint + model identity');
  assert.equal(state.mids.filter(mid => mid === 'shared-model').length, 2, 'distinct routes remain selectable');
  assert.deepEqual(state.endpointLabels, ['Same name', 'Same name'], 'same labels do not merge endpoint groups');
  assert.equal(state.favoriteRows, 1, 'favorite choice is not repeated in its source group');
  assert.match(state.storedFavorites[0], /^endpoint:/, 'legacy favorite migrated to a route-aware key');

  await evaluate(`(async () => {
    document.body.innerHTML = \`
      <div id="model-picker-wrap">
        <button id="model-picker-btn"><span id="model-picker-label">Select model</span></button>
        <div id="model-picker-menu" class="hidden">
          <div class="model-picker-search-row">
            <input id="model-picker-search">
            <button id="model-picker-refresh-btn"></button>
          </div>
          <div id="model-picker-list"></div>
        </div>
      </div>
      <textarea id="message"></textarea>
      <button id="scroll-bottom-btn"></button>
    \`;
    const mid = 'xiaomi/mimo-v2.5-pro/high';
    const personal = {
      endpoint_id:'mimo:xiaomi', endpoint_name:'Xiaomi', url:'mimo://acp',
      category:'api', models:[mid],
    };
    const shared = {
      endpoint_id:'shared:grant', endpoint_name:'MiMo shared', url:'mimo://acp',
      category:'api', models:[mid], shared:true, shared_by:'e',
    };
    window.__catalogItems = [personal, shared];
    await window.__catalogModels.refreshModels(true);
    const picker = await import('/static/js/modelPicker.js?route-sync=1');
    const sessions = [{
      id:'session-1', name:'Shared chat', model:mid,
      endpoint_url:'mimo://acp', endpoint_id:'shared:grant',
    }];
    window.__pickerSessions = sessions;
    window.__pickerCurrentSessionId = 'session-1';
    window.__pickerPending = null;
    window.__modelPicker = picker;
    picker.initModelPicker({
      getCurrentSessionId: () => window.__pickerCurrentSessionId,
      getSessions: () => sessions,
      getPendingChat: () => window.__pickerPending,
      setPendingChat: value => { window.__pickerPending = value; },
      createDirectChat: (url, modelId, endpointId) => {
        window.__pickerPending = { url, modelId, endpointId, source:'manual' };
      },
    });
    picker.updateModelPicker();
  })()`);
  await waitFor(
    "document.getElementById('model-picker-label').textContent.includes('mimo-v2.5-pro')",
    'shared session picker restoration',
  );

  await evaluate(`(async () => {
    window.__catalogItems = window.__catalogItems.filter(item => item.endpoint_id !== 'shared:grant');
    await window.__catalogModels.refreshModels(true);
  })()`);
  await waitFor(
    "document.getElementById('model-picker-label').textContent === 'Select model'",
    'revoked shared route no longer impersonated by personal route',
  );

  const revokedState = await evaluate(`(() => {
    document.getElementById('model-picker-btn').click();
    return {
      rows: document.querySelectorAll('.model-switch-item').length,
      endpoints: [...document.querySelectorAll('.model-switch-ep')].map(node => node.textContent),
    };
  })()`);
  assert.equal(revokedState.rows, 1, 'personal same-model route remains after shared route removal');
  assert.deepEqual(revokedState.endpoints, ['Xiaomi']);

  await evaluate(`(async () => {
    const mid = 'xiaomi/mimo-v2.5-pro/high';
    window.__catalogItems.push({
      endpoint_id:'shared:grant', endpoint_name:'MiMo shared', url:'mimo://acp',
      category:'api', models:[mid], shared:true, shared_by:'e',
    });
    await window.__catalogModels.refreshModels(true);
    const button = document.getElementById('model-picker-btn');
    if (document.getElementById('model-picker-menu').classList.contains('hidden')) button.click();
    const sharedRow = [...document.querySelectorAll('.model-switch-item')]
      .find(row => row.querySelector('.model-switch-ep')?.textContent.includes('Shared by e'));
    if (!sharedRow) throw new Error('shared picker row missing');
    sharedRow.click();
  })()`);
  await waitFor("window.__pickerPatch?.endpoint_id === 'shared:grant'", 'route-aware picker PATCH');
  const pickerState = await evaluate(`(() => ({
    patch: window.__pickerPatch,
    session: window.__pickerSessions[0],
    label: document.getElementById('model-picker-label').textContent,
    switchFencePending: Boolean(window.__odysseusModelSwitchPromise),
  }))()`);
  assert.equal(pickerState.patch.model, 'xiaomi/mimo-v2.5-pro/high');
  assert.equal(pickerState.patch.endpoint_url, 'mimo://acp');
  assert.equal(pickerState.session.endpoint_id, 'shared:grant');
  assert.equal(pickerState.session.model, 'xiaomi/mimo-v2.5-pro/high');
  assert.match(pickerState.label, /mimo-v2\.5-pro/);
  assert.equal(pickerState.switchFencePending, false, 'successful picker PATCH releases the chat send fence');

  const hierarchyState = JSON.parse(await evaluate(`(async () => {
    const baseModelId = 'openai/gpt-5.6-luna';
    const records = [
      { model_id:baseModelId, display_name:'gpt-5.6-luna', base_model_id:baseModelId },
      { model_id:baseModelId + '/low', display_name:'gpt-5.6-luna (low)', base_model_id:baseModelId, variant:'low' },
      { model_id:'openai/gpt-5.6-luna-fast', display_name:'gpt-5.6-luna Fast', base_model_id:baseModelId, preset:'fast' },
      { model_id:'openai/gpt-5.6-luna-fast/high', display_name:'gpt-5.6-luna Fast (high)', base_model_id:baseModelId, preset:'fast', variant:'high' },
      { model_id:'openai/gpt-5.6-luna-pro', display_name:'gpt-5.6-luna Pro', base_model_id:baseModelId, preset:'pro' },
    ];
    window.__catalogItems = [{
      endpoint_id:'mimo:openai', endpoint_name:'OpenAI', url:'mimo://acp',
      category:'api', catalog:records,
    }];
    await window.__catalogModels.refreshModels(true);
    const button = document.getElementById('model-picker-btn');
    if (document.getElementById('model-picker-menu').classList.contains('hidden')) button.click();
    const family = document.querySelector('.mp-model-family');
    const parentRows = [...document.getElementById('model-picker-list').children]
      .filter(node => node.classList.contains('mp-model-family'))
      .flatMap(node => [...node.children].filter(child => child.classList.contains('model-switch-item')));
    const optionsButton = family.querySelector('.mp-model-options-button');
    optionsButton.click();
    const mode = family.querySelector('select[aria-label$=" mode"]');
    const thinking = family.querySelector('select[aria-label$=" thinking"]');
    const modeLabels = [...mode.options].map(option => option.textContent);
    mode.value = 'fast';
    mode.dispatchEvent(new Event('change', { bubbles:true }));
    const thinkingLabels = [...thinking.options].map(option => option.textContent);
    thinking.value = 'high';
    family.querySelector('.mp-model-use-button').click();
    for (let i = 0; i < 50 && (
      window.__pickerPatch?.model !== 'openai/gpt-5.6-luna-fast/high'
      || window.__odysseusModelSwitchPromise
    ); i++) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    return JSON.stringify({
      familyCount:document.querySelectorAll('.mp-model-family').length,
      parentRows:parentRows.length,
      modeLabels,
      thinkingLabels,
      patch:window.__pickerPatch,
      switchFencePending:Boolean(window.__odysseusModelSwitchPromise),
    });
  })()`));
  assert.equal(hierarchyState.familyCount, 1, 'Luna presets and thinking variants render as one model family');
  assert.equal(hierarchyState.parentRows, 1, 'reasoning variants are not peer model rows');
  assert.deepEqual(hierarchyState.modeLabels, ['Standard', 'Fast preset', 'Pro preset']);
  assert.deepEqual(hierarchyState.thinkingLabels, ['Default', 'High']);
  assert.equal(hierarchyState.patch.endpoint_id, 'mimo:openai');
  assert.equal(hierarchyState.patch.model, 'openai/gpt-5.6-luna-fast/high');
  assert.equal(hierarchyState.switchFencePending, false);

  const routeSeparatedFamilies = await evaluate(`(async () => {
    const source = window.__catalogItems[0];
    window.__catalogItems = [source, {
      ...source,
      endpoint_id:'shared:grant',
      endpoint_name:'OpenAI shared',
      shared:true,
      shared_by:'e',
    }];
    await window.__catalogModels.refreshModels(true);
    const button = document.getElementById('model-picker-btn');
    if (document.getElementById('model-picker-menu').classList.contains('hidden')) button.click();
    return document.querySelectorAll('.mp-model-family').length;
  })()`);
  assert.equal(routeSeparatedFamilies, 2, 'personal and shared routes never merge into one model family');

  const validFavoriteState = JSON.parse(await evaluate(`(async () => {
    const key = 'odysseus-model-favorites:scope:browser-test';
    localStorage.setItem(key, JSON.stringify([
      'endpoint:' + encodeURIComponent('mimo:openai') + ':' + encodeURIComponent('openai/gpt-5.6-luna'),
    ]));
    const errors = [];
    const onError = event => errors.push(event.error?.message || event.message);
    window.addEventListener('error', onError);
    const menu = document.getElementById('model-picker-menu');
    if (!menu.classList.contains('hidden')) document.getElementById('model-picker-btn').click();
    await new Promise(resolve => setTimeout(resolve, 250));
    document.getElementById('model-picker-btn').click();
    await new Promise(resolve => setTimeout(resolve, 0));
    window.removeEventListener('error', onError);
    return JSON.stringify({
      errors,
      text:document.getElementById('model-picker-list').textContent,
    });
  })()`));
  assert.deepEqual(validFavoriteState.errors, [], 'a valid favorite does not crash picker rendering');
  assert.match(validFavoriteState.text, /Favorites/);

  const raceState = await evaluate(`(async () => {
    const pending = [];
    window.fetch = async input => {
      if (!String(input).includes('/api/models')) {
        return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
      }
      return new Promise(resolve => pending.push(resolve));
    };
    const older = window.__catalogModels.refreshModels(true);
    const newer = window.__catalogModels.refreshModels(true);
    pending[1](new Response(JSON.stringify({
      items:[{ endpoint_id:'newer', url:'https://newer.invalid/chat', models:['newer-model'] }],
    }), { status:200, headers:{'Content-Type':'application/json'} }));
    await newer;
    pending[0](new Response(JSON.stringify({
      items:[{ endpoint_id:'older', url:'https://older.invalid/chat', models:['older-model'] }],
    }), { status:200, headers:{'Content-Type':'application/json'} }));
    await older;
    return window.__catalogModels.getCachedItems().map(item => item.endpoint_id);
  })()`);
  assert.deepEqual(raceState, ['newer'], 'an older forced refresh cannot overwrite the winning catalogue');

  const defaultRaceState = await evaluate(`(async () => {
    let resolveDefault;
    window.__pickerCurrentSessionId = null;
    window.__pickerSessions.length = 0;
    window.__pickerPending = null;
    window.fetch = async input => {
      if (String(input).includes('/api/default-chat')) {
        return new Promise(resolve => { resolveDefault = resolve; });
      }
      return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    };
    window.__modelPicker.updateModelPicker();
    while (!resolveDefault) await new Promise(resolve => setTimeout(resolve, 0));
    window.__pickerPending = {
      url:'https://newer.invalid/chat', modelId:'newer-model',
      endpointId:'newer', source:'manual',
    };
    resolveDefault(new Response(JSON.stringify({
      endpoint_url:'https://default.invalid/chat',
      endpoint_id:'default',
      model:'default-model',
    }), { status:200, headers:{'Content-Type':'application/json'} }));
    await new Promise(resolve => setTimeout(resolve, 0));
    await new Promise(resolve => setTimeout(resolve, 0));
    return window.__pickerPending;
  })()`);
  assert.equal(defaultRaceState.endpointId, 'newer');
  assert.equal(defaultRaceState.modelId, 'newer-model', 'an in-flight default cannot overwrite a manual pick');

  const configuredDefaultState = JSON.parse(await evaluate(`(async () => {
    const deepseek = 'deepseek/deepseek-v4-pro';
    const xhigh = 'openai/gpt-5.6-sol/xhigh';
    const high = 'openai/gpt-5.6-sol/high';
    window.__pickerCurrentSessionId = null;
    window.__pickerSessions.length = 0;
    window.__pickerPending = null;
    window.__configuredDefaultModel = xhigh;
    window.__catalogItems = [
      {
        endpoint_id:'mimo:deepseek', endpoint_name:'DeepSeek', url:'mimo://acp',
        category:'api', models:[deepseek],
      },
      {
        endpoint_id:'mimo:openai', endpoint_name:'OpenAI', url:'mimo://acp',
        category:'api', models:[xhigh, high],
      },
    ];
    window.fetch = async input => {
      const url = String(input);
      if (url.includes('/api/models')) {
        return new Response(JSON.stringify({ items:window.__catalogItems }), {
          status:200, headers:{'Content-Type':'application/json'},
        });
      }
      if (url.includes('/api/default-chat')) {
        return new Response(JSON.stringify({
          endpoint_url:'mimo://acp', endpoint_id:'mimo:auto',
          model:window.__configuredDefaultModel,
        }), { status:200, headers:{'Content-Type':'application/json'} });
      }
      return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    };
    await window.__catalogModels.refreshModels(true);
    window.__modelPicker.updateModelPicker();
    for (let i = 0; i < 50 && window.__pickerPending?.modelId !== xhigh; i++) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    const initial = { ...window.__pickerPending };

    let resolveStaleDefault;
    let defaultCalls = 0;
    window.__pickerPending = null;
    window.fetch = async input => {
      const url = String(input);
      if (url.includes('/api/default-chat')) {
        defaultCalls += 1;
        if (defaultCalls === 1) {
          return new Promise(resolve => { resolveStaleDefault = resolve; });
        }
        return new Response(JSON.stringify({
          endpoint_url:'mimo://acp', endpoint_id:'mimo:auto', model:high,
        }), { status:200, headers:{'Content-Type':'application/json'} });
      }
      return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    };
    window.__modelPicker.updateModelPicker();
    while (!resolveStaleDefault) await new Promise(resolve => setTimeout(resolve, 0));
    window.__configuredDefaultModel = high;
    window.dispatchEvent(new CustomEvent('openclank:default-chat-changed'));
    resolveStaleDefault(new Response(JSON.stringify({
      endpoint_url:'mimo://acp', endpoint_id:'mimo:auto', model:xhigh,
    }), { status:200, headers:{'Content-Type':'application/json'} }));
    for (let i = 0; i < 50 && window.__pickerPending?.modelId !== high; i++) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    return JSON.stringify({ initial, changed:{ ...window.__pickerPending }, defaultCalls });
  })()`));
  assert.equal(configuredDefaultState.initial.endpointId, 'mimo:auto');
  assert.equal(
    configuredDefaultState.initial.modelId,
    'openai/gpt-5.6-sol/xhigh',
    'mimo:auto resolves its configured model instead of the first DeepSeek catalogue row',
  );
  assert.equal(
    configuredDefaultState.changed.modelId,
    'openai/gpt-5.6-sol/high',
    'saving a new default replaces an in-flight stale automatic selection',
  );
  assert.equal(configuredDefaultState.defaultCalls, 2, 'a settings change queues one fresh default lookup');

  const pendingState = await evaluate(`(async () => {
    const mid = 'xiaomi/mimo-v2.5-pro/high';
    localStorage.setItem('odysseus-workspace-id', 'workspace-pending');
    window.__pendingSessionBody = null;
    window.fetch = async (input, options = {}) => {
      const url = String(input);
      if (url.includes('/api/models')) {
        return new Response(JSON.stringify({ items:[{
          endpoint_id:'shared:grant', endpoint_name:'MiMo shared',
          url:'mimo://acp', category:'api', models:[mid], shared:true, shared_by:'e',
        }] }), { status:200, headers:{'Content-Type':'application/json'} });
      }
      if (options.method === 'POST' && url.includes('/api/session')) {
        window.__pendingSessionBody = Object.fromEntries(options.body.entries());
        return new Response(JSON.stringify({
          id:'created-session', name:'New Chat', model:mid, rag:false, archived:false,
          workspace_id:'workspace-pending',
        }), { status:200, headers:{'Content-Type':'application/json'} });
      }
      if (url.includes('/api/sessions')) return new Promise(() => {});
      return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    };
    await window.__catalogModels.refreshModels(true);
    const sessionsModule = await import('/static/js/sessions.js?pending-route-sync=1');
    sessionsModule.createDirectChat('mimo://acp', mid, 'shared:grant');
    const before = sessionsModule.getCurrentModel();
    const ok = await sessionsModule.materializePendingSession();
    const created = sessionsModule.getSessions().find(item => item.id === 'created-session');
    return { before, ok, created, body:window.__pendingSessionBody };
  })()`);
  assert.equal(pendingState.before, 'xiaomi/mimo-v2.5-pro/high');
  assert.equal(pendingState.ok, true);
  assert.equal(pendingState.created.endpoint_id, 'shared:grant');
  assert.equal(pendingState.created.endpoint_url, 'mimo://acp');
  assert.equal(pendingState.created.model, 'xiaomi/mimo-v2.5-pro/high');
  assert.equal(pendingState.body.workspace_id, 'workspace-pending',
    'pending chat materializes with its stable Workspace ID');
  assert.equal(pendingState.created.workspace_id, 'workspace-pending');
  process.stdout.write(JSON.stringify(state) + '\n');
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => chromium.once('exit', resolve));
  fs.rmSync(profile, { recursive: true, force: true });
}
