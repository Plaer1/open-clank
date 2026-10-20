#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const staticRoot = path.join(repository, 'static');

function chromeExecutable() {
  const configured = process.env.OPENCLANK_CHROME_BIN || process.env.CHROME_BIN;
  const candidates = [
    configured,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ].filter(Boolean);
  return candidates.find(candidate => fs.existsSync(candidate)) || '';
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      server.close(() => resolve(address.port));
    });
  });
}

function contentType(filename) {
  if (filename.endsWith('.js') || filename.endsWith('.mjs')) return 'text/javascript; charset=utf-8';
  if (filename.endsWith('.css')) return 'text/css; charset=utf-8';
  if (filename.endsWith('.svg')) return 'image/svg+xml';
  if (filename.endsWith('.json')) return 'application/json';
  return 'text/html; charset=utf-8';
}

async function startStaticServer() {
  const server = http.createServer((request, response) => {
    const requestUrl = new URL(request.url || '/', 'http://127.0.0.1');
    if (requestUrl.pathname === '/harness') {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      response.end('<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>');
      return;
    }
    if (!requestUrl.pathname.startsWith('/static/')) {
      response.writeHead(404);
      response.end('not found');
      return;
    }
    const relative = requestUrl.pathname.slice('/static/'.length);
    const candidate = path.resolve(staticRoot, relative);
    if (candidate !== staticRoot && !candidate.startsWith(`${staticRoot}${path.sep}`)) {
      response.writeHead(403);
      response.end('forbidden');
      return;
    }
    fs.readFile(candidate, (error, data) => {
      if (error) {
        response.writeHead(error.code === 'ENOENT' ? 404 : 500);
        response.end('not found');
        return;
      }
      response.writeHead(200, {
        'Cache-Control': 'no-store',
        'Content-Type': contentType(candidate),
      });
      response.end(data);
    });
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return { server, port: server.address().port };
}

function closeServer(server) {
  return new Promise(resolve => server.close(resolve));
}

const chrome = chromeExecutable();
if (!chrome) {
  process.stderr.write('No supported Chrome/Chromium executable found. Set OPENCLANK_CHROME_BIN.\n');
  process.exit(77);
}

const { server, port: staticPort } = await startStaticServer();
const debuggingPort = await freePort();
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-provider-add-models-'));
const browser = spawn(chrome, [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  '--disable-background-networking',
  '--disable-component-update',
  '--disable-default-apps',
  '--disable-sync',
  '--no-first-run',
  '--no-default-browser-check',
  `--remote-debugging-port=${debuggingPort}`,
  `--user-data-dir=${profile}`,
  'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  let targets;
  for (let attempt = 0; attempt < 160; attempt += 1) {
    if (browser.exitCode !== null) throw new Error(`Chrome exited during startup (${browser.exitCode})`);
    try {
      targets = await fetch(`http://127.0.0.1:${debuggingPort}/json`).then(response => response.json());
      if (targets?.length) break;
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  const target = targets?.find(item => item.type === 'page');
  assert(target?.webSocketDebuggerUrl, 'Chrome page target is unavailable');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });

  let sequence = 0;
  const pending = new Map();
  const runtimeErrors = [];
  const consoleErrors = [];
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const request = pending.get(message.id);
      pending.delete(message.id);
      clearTimeout(request.timer);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
      return;
    }
    if (message.method === 'Runtime.exceptionThrown') {
      const details = message.params?.exceptionDetails || {};
      runtimeErrors.push(details.exception?.description || details.text || 'browser exception');
    }
    if (message.method === 'Runtime.consoleAPICalled' && message.params?.type === 'error') {
      consoleErrors.push((message.params.args || []).map(item => item.value ?? item.description ?? '').join(' '));
    }
  });

  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => reject(new Error(`${method} timed out`)), 30_000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const result = await command('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    }
    return result.result.value;
  };
  const waitFor = async (expression, label, timeoutMs = 15_000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        if (await evaluate(expression)) return;
      } catch (_) {}
      await new Promise(resolve => setTimeout(resolve, 60));
    }
    throw new Error(`Timed out waiting for ${label}`);
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', { url: `http://127.0.0.1:${staticPort}/harness` });
  await waitFor("document.readyState === 'complete'", 'test origin');

  await evaluate(`(async () => {
    const html = await fetch('/static/index.html').then(response => response.text());
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    document.body.replaceChildren(parsed.getElementById('settings-modal'));
    const stylesheet = document.createElement('link');
    stylesheet.rel = 'stylesheet';
    stylesheet.href = '/static/style.css';
    document.head.appendChild(stylesheet);
    await new Promise(resolve => {
      stylesheet.addEventListener('load', resolve, { once:true });
      stylesheet.addEventListener('error', resolve, { once:true });
    });

    const harness = window.__providerAddModelsHarness = {
      requests: [],
      writes: [],
      errors: [],
      scrolledConnectionIds: [],
      coreResolvers: [],
      coreAborts: 0,
      optionalResolvers: [],
      holdCore: true,
      holdOptional: true,
      failFamilies: false,
      connectionSerial: 0,
      accountSerial: 0,
      ownedShares: [],
      connections: [{
        id:'pc-existing', family_id:'anthropic', adapter_id:'anthropic-messages',
        kind:'official', billing_lane:'metered_api', label:'Existing Anthropic',
        url:null, settings:{}, enabled:true, revision:4,
      }],
      models: [{
        id:'pmr-existing', connection_id:'pc-existing', model_id:'claude-existing',
        display_name:'Claude Existing', operations:['chat.stream','chat.complete'],
        capabilities:{}, visibility:'visible', enabled:true, revision:2,
      }],
      accounts: new Map([['pc-existing', [{
        id:'pac-existing', connection_id:'pc-existing', label:'Existing account',
        auth_method:'api_key', auth_class:'metered', order:0, enabled:true,
        identity:{}, revision:3,
      }]]]),
      families: [
        {
          id:'openai', display_name:'OpenAI', kinds:['official','custom_gateway'],
          adapters:['openai-responses'], billing_lanes:['metered_api','custom'], model_count:2,
          auth_methods:[{ id:'api_key', type:'api', label:'API key' }],
        },
        {
          id:'ollama', display_name:'Ollama', kinds:['local'], adapters:['ollama'],
          billing_lanes:['local'], model_count:1,
          auth_methods:[{ id:'none', type:'none', label:'No API key' }],
        },
        {
          id:'lmstudio', display_name:'LM Studio', kinds:['local'], adapters:['openai-compatible'],
          billing_lanes:['local'], model_count:3,
          auth_methods:[{ id:'none', type:'none', label:'No API key' }],
        },
        {
          id:'anthropic', display_name:'Anthropic', kinds:['official'],
          adapters:['anthropic-messages'], billing_lanes:['metered_api'], model_count:1,
          auth_methods:[{ id:'api_key', type:'api', label:'API key' }],
        },
        {
          id:'github-copilot', display_name:'GitHub Copilot', kinds:['subscription'],
          adapters:['github-copilot'], billing_lanes:['subscription'], model_count:2,
          auth_methods:[], auth_methods_complete:false,
        },
      ],
    };
    window.addEventListener('error', event => harness.errors.push(event.error?.stack || event.message));
    window.addEventListener('unhandledrejection', event => harness.errors.push(event.reason?.stack || String(event.reason)));
    const nativeScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function(options) {
      if (this?.dataset?.providerConnectionId) harness.scrolledConnectionIds.push(this.dataset.providerConnectionId);
      return nativeScrollIntoView?.call(this, options);
    };
    window._isAdmin = false;
    window.modelsModule = { refreshModels: async () => {} };
    window.sessionModule = { updateModelPicker() {} };
    window.confirm = () => true;

    const json = (body, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers:{ 'Content-Type':'application/json' },
    });
    const generic = pathname => {
      if (pathname === '/api/auth/status') return { configured:true, authenticated:true, username:'browser-test', is_admin:false };
      if (pathname === '/api/auth/policy') return { password_min_length:8 };
      if (pathname === '/api/auth/integrations') return { integrations:[] };
      if (pathname === '/api/calendar/config/accounts' || pathname === '/api/email/accounts') return { accounts:[] };
      if (pathname === '/api/contacts/list') return { contacts:[], count:0 };
      if (pathname === '/api/calendar/calendars') return { calendars:[] };
      if (pathname === '/api/presets/templates') return [];
      if (pathname === '/api/presets/default-persona') return { name:'Open Clank', system_prompt:'' };
      if (pathname === '/api/models') return { items:[
        {
          endpoint_id:'pc-existing', endpoint_name:'Existing Anthropic',
          endpoint_kind:'official', billing_lane:'metered_api',
          catalog:[{
            model_id:'claude-existing', provider_model_id:'claude-existing',
            provider_model_route_id:'pmr-existing', display_name:'Claude Existing',
            operations:['chat.stream','chat.complete'], capabilities:{ chat:true },
          }],
        },
        {
          endpoint_id:'share:grant-browser', endpoint_name:'Shared GPT · shared by friend',
          endpoint_kind:'shared', shared:true, shared_by:'friend',
          catalog:[{
            model_id:'gpt-shared', provider_model_id:'gpt-shared',
            display_name:'Shared GPT', operations:['chat.stream','chat.complete'],
            capabilities:{ chat:true },
          }],
        },
      ] };
      return {};
    };
    window.fetch = async (input, init = {}) => {
      const requestUrl = new URL(String(input), location.origin);
      const pathname = requestUrl.pathname;
      const method = String(init.method || 'GET').toUpperCase();
      harness.requests.push({ pathname, method });
      if (!pathname.startsWith('/api/v1/providers')) return json(generic(pathname));
      const suffix = pathname.slice('/api/v1/providers'.length) || '/';
      if (suffix === '/families' && method === 'GET') {
        if (harness.failFamilies) return json({ detail:'catalogue temporarily unavailable' }, 503);
        return json({ schema_version:1, families:harness.families });
      }
      const authMethodsMatch = suffix.match(new RegExp('^/families/([^/]+)/auth-methods$'));
      if (authMethodsMatch && method === 'POST') {
        const familyId = decodeURIComponent(authMethodsMatch[1]);
        const family = harness.families.find(item => item.id === familyId);
        if (!family) return json({ detail:'Unknown provider family' }, 404);
        const authMethods = [{ id:'oauth:0', type:'oauth', label:'GitHub device login' }];
        return json({
          family_id:familyId,
          auth_methods:authMethods,
          auth_methods_complete:true,
        });
      }
      if (suffix === '/management-snapshot' && method === 'GET') {
        const snapshot = () => json({
          schema_version:1,
          connections:harness.connections.map(connection => ({
            ...connection,
            account_count:(harness.accounts.get(connection.id) || []).length,
          })),
          models:harness.models,
          shares:{ received:[] },
          deferred:['accounts','bindings','owned_shares','recipients'],
        });
        if (!harness.holdCore) return snapshot();
        return new Promise((resolve, reject) => {
          const release = () => {
            init.signal?.removeEventListener?.('abort', abort);
            resolve(snapshot());
          };
          const abort = () => {
            harness.coreResolvers = harness.coreResolvers.filter(item => item !== release);
            harness.coreAborts += 1;
            reject(new DOMException('Superseded provider snapshot', 'AbortError'));
          };
          harness.coreResolvers.push(release);
          if (init.signal?.aborted) abort();
          else init.signal?.addEventListener?.('abort', abort, { once:true });
        });
      }
      if (suffix === '/connections' && method === 'GET') {
        if (!harness.holdCore) return json({ connections:harness.connections });
        return new Promise(resolve => harness.coreResolvers.push(() => resolve(json({ connections:harness.connections }))));
      }
      if (suffix === '/connections' && method === 'POST') {
        const body = JSON.parse(init.body || '{}');
        harness.writes.push({ kind:'connection', body });
        const connection = {
          id:'pc-created-' + (++harness.connectionSerial),
          ...body,
          revision:1,
        };
        harness.connections.push(connection);
        const primaryModel = {
          id:'pmr-' + connection.id,
          connection_id:connection.id,
          model_id:body.family_id === 'ollama' ? 'llama-local' : 'gpt-test',
          display_name:body.family_id === 'ollama' ? 'Llama Local' : 'GPT Test',
          operations:['chat.stream','chat.complete'], capabilities:{},
          visibility:'visible', enabled:true, revision:1,
        };
        harness.models.push(primaryModel);
        if (body.family_id === 'openai') {
          for (let index = 2; index <= 47; index += 1) {
            harness.models.push({
              ...primaryModel,
              id:'pmr-' + connection.id + '-' + index,
              model_id:'openai-fixture-' + String(index).padStart(2, '0'),
              display_name:'OpenAI Fixture ' + String(index).padStart(2, '0'),
            });
          }
        }
        return json(connection, 201);
      }
      const accountMatch = suffix.match(new RegExp('^/connections/([^/]+)/accounts$'));
      if (accountMatch && method === 'GET') {
        return json({ accounts:harness.accounts.get(decodeURIComponent(accountMatch[1])) || [] });
      }
      if (accountMatch && method === 'POST') {
        const connectionId = decodeURIComponent(accountMatch[1]);
        const body = JSON.parse(init.body || '{}');
        harness.writes.push({ kind:'account', connectionId, body });
        const account = {
          id:'pac-created-' + (++harness.accountSerial), connection_id:connectionId,
          label:body.label, auth_method:'api_key', auth_class:'metered', order:0,
          enabled:true, identity:{}, revision:1,
        };
        harness.accounts.set(connectionId, [...(harness.accounts.get(connectionId) || []), account]);
        return json(account, 201);
      }
      if (suffix === '/models' && method === 'GET') {
        if (!harness.holdCore) return json({ models:harness.models });
        return new Promise(resolve => harness.coreResolvers.push(() => resolve(json({ models:harness.models }))));
      }
      const connectionEligibility = suffix.match(new RegExp('^/connections/([^/]+)/eligibility$'));
      if (connectionEligibility) {
        const connectionId = decodeURIComponent(connectionEligibility[1]);
        return json({
          connection_id:connectionId,
          models:harness.models
            .filter(model => model.connection_id === connectionId)
            .map(model => ({ model_route_id:model.id, accounts:[] })),
        });
      }
      if (suffix.includes('/eligibility')) return json({ accounts:[] });
      if (suffix === '/bindings') return json({ detail:'routing temporarily unavailable' }, 503);
      if (suffix === '/share-recipients') return json({ recipients:[{ username:'friend' }] });
      if (suffix === '/shares/received') return json({ shares:[] });
      if (suffix === '/shares') {
        if (!harness.holdOptional) return json({ shares:harness.ownedShares });
        return new Promise(resolve => harness.optionalResolvers.push(() => resolve(json({ shares:harness.ownedShares }))));
      }
      const shareMatch = suffix.match(new RegExp('^/models/([^/]+)/shares/([^/]+)$'));
      if (shareMatch && method === 'PUT') {
        const modelRouteId = decodeURIComponent(shareMatch[1]);
        const recipient = decodeURIComponent(shareMatch[2]);
        const body = JSON.parse(init.body || '{}');
        harness.writes.push({ kind:'share', modelRouteId, recipient, body });
        harness.ownedShares = harness.ownedShares.filter(share => !(
          share.recipient === recipient
          && share.model_selector?.model_route_ids?.includes(modelRouteId)
        ));
        if (body.enabled) harness.ownedShares.push({
          id:'psg-browser-' + recipient,
          recipient,
          connection_id:harness.models.find(item => item.id === modelRouteId)?.connection_id,
          billing_lane:'metered_api',
          label:'Shared models',
          account_selector:{ mode:'all_live_accounts' },
          model_selector:{ mode:'explicit_models', model_route_ids:[modelRouteId] },
          disclosure_fields:[],
          state:'active',
          revision:1,
        });
        return json({ enabled:Boolean(body.enabled), share:body.enabled ? harness.ownedShares.at(-1) : null });
      }
      return json({});
    };

    const settings = await import('/static/js/settings.js');
    const providerControl = await import('/static/js/providerControl.js');
    harness.openedAt = performance.now();
    settings.open('services');
    harness.pendingLoad = providerControl.load();
  })()`);

  await waitFor(
    "document.querySelectorAll('.provider-control-quick-form').length === 2",
    'provider catalog first paint',
  );
  const catalogFirstState = await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    const providerRequests = h.requests.filter(item => item.pathname.startsWith('/api/v1/providers'));
    const catalogIndex = providerRequests.findIndex(item => item.pathname === '/api/v1/providers/families');
    let cache = null;
    try { cache = JSON.parse(sessionStorage.getItem('open-clank:provider-families:v1')); } catch (_) {}
    return {
      firstWave:providerRequests.slice(catalogIndex, catalogIndex + 1).map(item => item.pathname),
      quickForms:document.querySelectorAll('.provider-control-quick-form').length,
      existingRows:document.querySelectorAll('[data-provider-connection-id]').length,
      heldCore:h.coreResolvers.length,
      connectionReads:providerRequests.filter(item => item.pathname === '/api/v1/providers/connections').length,
      modelReads:providerRequests.filter(item => item.pathname === '/api/v1/providers/models').length,
      optionalPending:h.optionalResolvers.length,
      cachedFormat:cache?.format,
      cachedSchema:cache?.schema_version,
      cachedFamilies:cache?.families?.length,
      firstUsablePaintMs:performance.now() - h.openedAt,
    };
  })()`);
  assert.deepEqual(catalogFirstState.firstWave, [
    '/api/v1/providers/families',
  ], 'Add Models first paint starts only the family catalogue');
  assert.equal(catalogFirstState.quickForms, 2, 'Add Models becomes usable from one catalogue read');
  assert.equal(catalogFirstState.existingRows, 0, 'Add Models does not construct Added Models rows');
  assert.equal(catalogFirstState.heldCore, 0, 'Add Models does not start owner management reads');
  assert.equal(catalogFirstState.connectionReads, 0, 'opening Add Models does not read connections');
  assert.equal(catalogFirstState.modelReads, 0, 'opening Add Models does not read models');
  assert.equal(catalogFirstState.optionalPending, 0, 'hidden advanced requests do not compete with the first usable Add paint');
  assert.equal(catalogFirstState.cachedFormat, 1, 'validated family cache records its format');
  assert.equal(catalogFirstState.cachedSchema, 1, 'validated family cache records the server schema');
  assert.equal(catalogFirstState.cachedFamilies, 5, 'the live family catalog is cached for an immediate repeat paint');
  assert.ok(catalogFirstState.firstUsablePaintMs <= 500,
    `cached/catalogue first usable paint stays within 500ms (${catalogFirstState.firstUsablePaintMs.toFixed(1)}ms)`);

  const incompleteAuthState = await evaluate(`(() => {
    const remote = document.querySelector('[data-provider-add-mode="remote"]');
    const trigger = remote.querySelector('[role="combobox"]');
    const menu = document.getElementById(trigger.getAttribute('aria-controls'));
    trigger.click();
    menu.querySelector('[data-value="github-copilot"]')?.click();
    const submit = remote.querySelector('button[type="submit"]');
    const enrich = [...remote.querySelectorAll('button')]
      .find(button => button.textContent.trim() === 'Load sign-in methods');
    const before = window.__providerAddModelsHarness.requests
      .filter(item => item.pathname.endsWith('/families/github-copilot/auth-methods')).length;
    enrich?.click();
    return {
      submitDisabled:Boolean(submit?.disabled),
      submitText:submit?.textContent.trim(),
      enrichmentControl:Boolean(enrich),
      authReadsBefore:before,
    };
  })()`);
  assert.deepEqual(incompleteAuthState, {
    submitDisabled:true,
    submitText:'Sign-in required',
    enrichmentControl:true,
    authReadsBefore:0,
  }, 'an incomplete catalogue family fails closed instead of becoming a fake keyless provider');
  await waitFor(
    "document.querySelector('[data-provider-add-mode=\"remote\"] button[type=\"submit\"]')?.textContent.trim() === 'GitHub device login' && !document.querySelector('[data-provider-add-mode=\"remote\"] button[type=\"submit\"]')?.disabled",
    'explicit provider auth-method enrichment',
  );
  const enrichedAuthState = await evaluate(`(() => {
    const remote = document.querySelector('[data-provider-add-mode="remote"]');
    const trigger = remote.querySelector('[role="combobox"]');
    const menu = document.getElementById(trigger.getAttribute('aria-controls'));
    const authReads = window.__providerAddModelsHarness.requests
      .filter(item => item.pathname.endsWith('/families/github-copilot/auth-methods')).length;
    trigger.click();
    menu.querySelector('[data-value="openai"]')?.click();
    return {
      authReads,
      restoredProvider:trigger.textContent.includes('OpenAI'),
      restoredSubmit:remote.querySelector('button[type="submit"]')?.textContent.trim(),
    };
  })()`);
  assert.deepEqual(enrichedAuthState, {
    authReads:1,
    restoredProvider:true,
    restoredSubmit:'Add',
  }, 'explicit enrichment installs sanitized sign-in methods and can return to the original provider');

  const stableFamilyRefresh = await evaluate(`(async () => {
    const local = document.querySelector('[data-provider-add-mode="local"] .provider-control-url-input');
    const secret = document.querySelector('[data-provider-add-mode="remote"] input[type="password"]');
    local.value = 'http://localhost:11999';
    secret.value = 'in-progress-secret';
    secret.focus();
    const before = window.__providerAddModelsHarness.requests
      .filter(item => item.pathname === '/api/v1/providers/families').length;
    const providers = await import('/static/js/providerControl.js');
    await providers.load({ force:true, view:'services' });
    return {
      familyReads:window.__providerAddModelsHarness.requests
        .filter(item => item.pathname === '/api/v1/providers/families').length - before,
      localDraft:document.querySelector('[data-provider-add-mode="local"] .provider-control-url-input')?.value,
      secretDraft:document.querySelector('[data-provider-add-mode="remote"] input[type="password"]')?.value,
      focusPreserved:document.activeElement === document.querySelector('[data-provider-add-mode="remote"] input[type="password"]'),
    };
  })()`);
  assert.deepEqual(stableFamilyRefresh, {
    familyReads:1,
    localDraft:'http://localhost:11999',
    secretDraft:'in-progress-secret',
    focusPreserved:true,
  }, 'an unchanged family refresh preserves in-progress Add Models input and focus');

  const failedFamilyState = await evaluate(`(async () => {
    const h = window.__providerAddModelsHarness;
    const providers = await import('/static/js/providerControl.js');
    const before = h.requests.filter(item => item.pathname === '/api/v1/providers/families').length;
    h.failFamilies = true;
    await providers.load({ force:true, view:'services' });
    const cachedSurfaceStayedInteractive = document.querySelectorAll('.provider-control-quick-form').length === 2;
    h.failFamilies = false;
    await providers.load({ view:'services' });
    return {
      cachedSurfaceStayedInteractive,
      familyReads:h.requests.filter(item => item.pathname === '/api/v1/providers/families').length - before,
    };
  })()`);
  assert.deepEqual(failedFamilyState, { cachedSurfaceStayedInteractive:true, familyReads:2 },
    'a failed family refresh preserves the cached Add surface and is not marked fresh before retry');

  await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    document.querySelector('[data-provider-add-mode="local"] .provider-control-url-input').value = 'http://localhost:11999';
    document.querySelector('[data-provider-add-mode="remote"] input[type="password"]').value = 'in-progress-secret';
    document.querySelector('[data-settings-tab="added-models"]').click();
    import('/static/js/providerControl.js').then(providers => {
      h.pendingLoad = providers.load({ view:'added-models' });
    });
  })()`);
  await waitFor(
    "window.__providerAddModelsHarness.coreResolvers.length >= 1",
    'held Added Models management snapshot',
  );
  const addedPendingState = await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    const providerRequests = h.requests.filter(item => item.pathname.startsWith('/api/v1/providers'));
    return {
      managementReads:providerRequests.filter(item => item.pathname === '/api/v1/providers/management-snapshot').length,
      connectionReads:providerRequests.filter(item => item.pathname === '/api/v1/providers/connections').length,
      modelReads:providerRequests.filter(item => item.pathname === '/api/v1/providers/models').length,
      existingRows:document.querySelectorAll('[data-provider-connection-id]').length,
      optionalPending:h.optionalResolvers.length,
    };
  })()`);
  assert.deepEqual(addedPendingState, {
    managementReads:1,
    connectionReads:0,
    modelReads:0,
    existingRows:0,
    optionalPending:0,
  }, 'Added Models owns one bounded snapshot and no hidden advanced reads');

  await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    h.holdCore = false;
    h.coreReleasedAt = performance.now();
    h.coreResolvers.splice(0).forEach(resolve => resolve());
  })()`);

  await waitFor(
    "document.querySelector('[data-provider-connection-id=\"pc-existing\"]')?.textContent.includes('Existing Anthropic')",
    'existing connection after the bounded Added Models core wave',
  );
  const progressiveState = await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    const addedTab = document.querySelector('[data-settings-tab="added-models"]');
    const servicesPanel = document.querySelector('[data-settings-panel="services"]');
    return {
      addedTabText:addedTab?.textContent.trim(),
      addedTabVisible:Boolean(addedTab && getComputedStyle(addedTab).display !== 'none' && addedTab.getBoundingClientRect().height > 0),
      servicesPanelVisible:Boolean(servicesPanel && !servicesPanel.classList.contains('hidden')),
      existingText:document.querySelector('[data-provider-connection-id="pc-existing"]')?.textContent || '',
      optionalPending:h.optionalResolvers.length,
      eligibilityRequests:h.requests.filter(item => item.pathname.includes('/eligibility')).length,
      corePaintMs:performance.now() - h.coreReleasedAt,
    };
  })()`);
  assert.equal(progressiveState.addedTabText, 'Added Models');
  assert.equal(progressiveState.addedTabVisible, true, 'Added Models is a visible first-class settings tab');
  assert.equal(progressiveState.servicesPanelVisible, false, 'Added Models is now the visible panel');
  assert.match(progressiveState.existingText, /Existing Anthropic/);
  assert.doesNotMatch(progressiveState.existingText, /Claude Existing/,
    'collapsed rows do not construct hidden model bodies');
  assert.equal(progressiveState.optionalPending, 0, 'advanced reads stay lazy after core paint');
  assert.equal(progressiveState.eligibilityRequests, 0, 'initial load does not fan out per-model eligibility requests');
  assert.ok(progressiveState.corePaintMs <= 1000,
    `released DB-only management state paints within one second (${progressiveState.corePaintMs.toFixed(1)}ms)`);

  await evaluate(`(async () => {
    const h = window.__providerAddModelsHarness;
    h.holdCore = true;
    h.overlapSnapshotStart = h.requests
      .filter(item => item.pathname === '/api/v1/providers/management-snapshot').length;
    const providers = await import('/static/js/providerControl.js');
    h.overlapFirst = providers.load({ force:true, view:'added-models' });
  })()`);
  await waitFor(
    "window.__providerAddModelsHarness.coreResolvers.length === 1",
    'first overlapping management snapshot',
  );
  await evaluate(`(async () => {
    const h = window.__providerAddModelsHarness;
    const providers = await import('/static/js/providerControl.js');
    h.overlapSecond = providers.load({ force:true, view:'added-models' });
  })()`);
  await waitFor(
    "window.__providerAddModelsHarness.coreAborts === 1 && window.__providerAddModelsHarness.coreResolvers.length === 1",
    'superseded snapshot abort and replacement',
  );
  const overlapState = await evaluate(`(async () => {
    const h = window.__providerAddModelsHarness;
    h.holdCore = false;
    h.coreResolvers.splice(0).forEach(resolve => resolve());
    await Promise.all([h.overlapFirst, h.overlapSecond]);
    return {
      emitted:h.requests.filter(item => item.pathname === '/api/v1/providers/management-snapshot').length
        - h.overlapSnapshotStart,
      aborted:h.coreAborts,
      pending:h.coreResolvers.length,
    };
  })()`);
  assert.deepEqual(overlapState, { emitted:2, aborted:1, pending:0 },
    'real Chrome aborts the superseded force wave and completes only its replacement');

  const invalidationState = await evaluate(`(async () => {
    const h = window.__providerAddModelsHarness;
    const before = h.requests
      .filter(item => item.pathname === '/api/v1/providers/management-snapshot').length;
    document.dispatchEvent(new CustomEvent('open-clank:providers-updated'));
    const providers = await import('/static/js/providerControl.js');
    await providers.load({ view:'added-models' });
    return {
      snapshotReads:h.requests
        .filter(item => item.pathname === '/api/v1/providers/management-snapshot').length - before,
    };
  })()`);
  assert.deepEqual(invalidationState, { snapshotReads:1 },
    'one external provider-update invalidation produces exactly one next-view snapshot');

  const beforeAi = await evaluate(`(() => {
    const requests = window.__providerAddModelsHarness.requests;
    return {
      catalog:requests.filter(item => item.pathname === '/api/models').length,
      endpointOptions:document.getElementById('set-defaultEpSelect')?.options.length || 0,
    };
  })()`);
  assert.equal(beforeAi.endpointOptions, 0, 'hidden AI model controls remain uninitialized on Add Models');

  await evaluate(`(() => document.querySelector('[data-settings-tab="ai"]').click())()`);
  await waitFor(
    `window.__providerAddModelsHarness.requests.filter(item => item.pathname === '/api/models').length >= ${beforeAi.catalog + 1}`,
    'one lazy AI endpoint snapshot',
  );
  await waitFor(
    "document.getElementById('set-defaultEpSelect')?.options.length > 0",
    'lazy AI model controls',
  );
  const afterAi = await evaluate(`(() => {
    const requests = window.__providerAddModelsHarness.requests;
    return {
      catalog:requests.filter(item => item.pathname === '/api/models').length,
      endpointText:document.getElementById('set-defaultEpSelect')?.textContent || '',
      memoryEndpointText:document.getElementById('set-memoryEpSelect')?.textContent || '',
      memoryModelText:document.getElementById('set-memoryModelSelect')?.textContent || '',
    };
  })()`);
  assert.equal(afterAi.catalog - beforeAi.catalog, 1,
    'first AI visit adds exactly one owner-visible catalog read');
  assert.match(afterAi.endpointText, /Existing Anthropic/,
    'lazy initialization preserves populated AI model controls');
  assert.match(afterAi.endpointText, /Shared GPT/,
    'shared models are selectable in AI Defaults');
  assert.match(afterAi.memoryEndpointText, /Same as Utility/,
    'Memory endpoint defaults to dynamic Utility inheritance');
  assert.match(afterAi.memoryEndpointText, /Shared GPT/,
    'Memory can select a shared model independently');
  assert.match(afterAi.memoryModelText, /Same as Utility/,
    'Memory model exposes the inheritance choice');
  await evaluate(`(() => document.querySelector('[data-settings-tab="services"]').click())()`);

  await evaluate(`(() => {
    document.querySelector('[data-settings-tab="added-models"]').click();
  })()`);
  await waitFor(
    "document.querySelector('[data-settings-panel=\"added-models\"]') && !document.querySelector('[data-settings-panel=\"added-models\"]')?.classList.contains('hidden')",
    'Added Models tab navigation',
  );
  const navigationState = await evaluate(`(() => ({
    addedActive:document.querySelector('[data-settings-tab="added-models"]')?.classList.contains('active'),
    addedVisible:getComputedStyle(document.querySelector('[data-settings-panel="added-models"]')).display !== 'none',
    servicesHidden:document.querySelector('[data-settings-panel="services"]')?.classList.contains('hidden'),
    existingStillPresent:Boolean(document.querySelector('[data-provider-connection-id="pc-existing"]')),
  }))()`);
  assert.deepEqual(navigationState, {
    addedActive:true,
    addedVisible:true,
    servicesHidden:true,
    existingStillPresent:true,
  });

  await evaluate(`(async () => {
    const h = window.__providerAddModelsHarness;
    h.holdOptional = false;
    h.optionalResolvers.splice(0).forEach(resolve => resolve());
    await h.pendingLoad;
  })()`);
  const resilientState = await evaluate(`(() => ({
    connectionVisible:Boolean(document.querySelector('[data-provider-connection-id="pc-existing"]')),
    status:document.getElementById('provider-control-added-status')?.textContent || '',
    routingRequests:window.__providerAddModelsHarness.requests.filter(item => item.pathname.endsWith('/bindings')).length,
    eligibilityRequests:window.__providerAddModelsHarness.requests.filter(item => item.pathname.includes('/eligibility')).length,
    localDraft:document.querySelector('[data-provider-add-mode="local"] .provider-control-url-input')?.value || '',
    secretDraft:document.querySelector('[data-provider-add-mode="remote"] input[type="password"]')?.value || '',
  }))()`);
  assert.equal(resilientState.connectionVisible, true, 'lazy advanced state leaves core connections intact');
  assert.equal(resilientState.routingRequests, 0, 'collapsed routing makes no request');
  assert.doesNotMatch(resilientState.status, /routing temporarily unavailable/);
  assert.equal(resilientState.eligibilityRequests, 0);
  assert.equal(resilientState.localDraft, 'http://localhost:11999', 'progressive background reads preserve a typed endpoint');
  assert.equal(resilientState.secretDraft, 'in-progress-secret', 'progressive background reads preserve a typed credential draft');

  await evaluate(`(() => {
    document.querySelector('[data-provider-add-mode="local"] .provider-control-url-input').value = 'http://localhost:11434';
    document.querySelector('[data-provider-add-mode="remote"] input[type="password"]').value = '';
  })()`);

  await evaluate(`(() => document.querySelector('[data-settings-tab="services"]').click())()`);
  await waitFor("document.querySelectorAll('.provider-control-quick-form').length === 2", 'compact local and API add forms');
  const formState = await evaluate(`(() => {
    const forms = [...document.querySelectorAll('.provider-control-quick-form')];
    const remote = document.querySelector('[data-provider-add-mode="remote"]');
    const local = document.querySelector('[data-provider-add-mode="local"]');
    const picker = local?.querySelector('[role="combobox"]');
    const listbox = picker && document.getElementById(picker.getAttribute('aria-controls'));
    const localPickerRect = picker?.getBoundingClientRect();
    const localUrlRect = local?.querySelector('.provider-control-url-shell input')?.getBoundingClientRect();
    const localRect = local?.getBoundingClientRect();
    const remoteRect = remote?.getBoundingClientRect();
    const remotePicker = remote?.querySelector('[role="combobox"]');
    const remotePickerRect = remotePicker?.getBoundingClientRect();
    const remoteComboRect = remote?.querySelector('.provider-control-provider-combo')?.getBoundingClientRect();
    const remoteHint = remote?.querySelector('.provider-control-route-hint');
    const remoteHintRect = remoteHint?.getBoundingClientRect();
    const pickerStyle = picker && getComputedStyle(picker);
    const headingStyles = [...forms].map(form => getComputedStyle(form.querySelector('.provider-control-add-heading')));
    const cardStyles = forms.map(form => getComputedStyle(form));
    const ownershipLabels = [...document.querySelectorAll(
      '[data-settings-panel="services"] > .settings-scope-label, [data-settings-panel="added-models"] > .settings-scope-label',
    )];
    return {
      modes:forms.map(form => form.dataset.providerAddMode).sort(),
      nativeSelects:document.querySelectorAll('[data-settings-panel="services"] select, [data-settings-panel="added-models"] select').length,
      remoteText:remote?.textContent || '',
      localText:local?.textContent || '',
      remoteSecretLabel:remote?.querySelector('input[type="password"]')?.getAttribute('aria-label'),
      remoteSecretRequired:Boolean(remote?.querySelector('input[type="password"]')?.required),
      addTexts:forms.map(form => form.querySelector('button[type="submit"]')?.textContent),
      pickerRole:picker?.getAttribute('role'),
      pickerExpanded:picker?.getAttribute('aria-expanded'),
      pickerHaspopup:picker?.getAttribute('aria-haspopup'),
      listboxRole:listbox?.getAttribute('role'),
      listboxLabel:listbox?.getAttribute('aria-label'),
      optionRoles:[...(listbox?.querySelectorAll('[role="option"]') || [])].map(option => option.getAttribute('role')),
      customPickerVisual:{
        appearance:pickerStyle?.appearance,
        display:pickerStyle?.display,
        height:localPickerRect?.height || 0,
        radius:pickerStyle?.borderRadius,
        hasProviderLogo:Boolean(picker?.querySelector('.provider-control-picker-icon svg')),
        hasCaret:Boolean(picker?.querySelector('.provider-control-picker-caret-shell svg')),
        localCompoundRow:Boolean(localPickerRect && localUrlRect
          && Math.abs(localPickerRect.top - localUrlRect.top) <= 1
          && Math.abs(localPickerRect.height - localUrlRect.height) <= 1
          && Math.abs(localPickerRect.right - localUrlRect.left) <= 1),
        pickerWidthRatio:localPickerRect && localRect ? localPickerRect.width / localRect.width : 1,
        remotePickerWidth:remotePickerRect?.width || 0,
        remotePickerWidthRatio:remotePickerRect && remoteComboRect ? remotePickerRect.width / remoteComboRect.width : 1,
        remoteHintText:remoteHint?.textContent.trim() || '',
        remoteHintDisplay:remoteHint ? getComputedStyle(remoteHint).display : '',
        remoteHintVisible:Boolean(remoteHintRect?.width && remoteHintRect?.height),
        remoteHintWidth:remoteHintRect?.width || 0,
        remoteComboWidth:remoteComboRect?.width || 0,
      },
      cardFlowVisual:{
        stacked:Boolean(localRect && remoteRect && localRect.bottom < remoteRect.top),
        headingDividers:headingStyles.map(style => style.borderBottomStyle),
        headingIcons:forms.map(form => Boolean(form.querySelector('.provider-control-add-title-icon svg'))),
        cardBorders:cardStyles.map(style => style.borderTopStyle),
        cardPadding:cardStyles.map(style => Number.parseFloat(style.paddingTop)),
      },
      quickPickerSearches:document.querySelectorAll('[data-provider-add-mode] .provider-control-picker-search').length,
      ownershipLabels:ownershipLabels.map(label => getComputedStyle(label).display),
    };
  })()`);
  assert.deepEqual(formState.modes, ['local', 'remote']);
  assert.equal(formState.nativeSelects, 0, 'active provider UI contains no native select elements');
  assert.match(formState.remoteText, /Add API Models/);
  assert.equal(formState.remoteSecretLabel, 'API key');
  assert.equal(formState.remoteSecretRequired, true);
  assert.match(formState.localText, /Add Local Models/);
  assert.deepEqual(formState.addTexts, ['Add', 'Add']);
  assert.equal(formState.pickerRole, 'combobox');
  assert.equal(formState.pickerExpanded, 'false');
  assert.equal(formState.pickerHaspopup, 'listbox');
  assert.equal(formState.listboxRole, 'listbox');
  assert.match(formState.listboxLabel, /local model provider/i);
  assert.ok(formState.optionRoles.length >= 2);
  assert.ok(formState.optionRoles.every(role => role === 'option'));
  assert.equal(formState.customPickerVisual.appearance, 'none', 'family picker opts out of browser-native control chrome');
  assert.equal(formState.customPickerVisual.display, 'flex');
  assert.ok(formState.customPickerVisual.height >= 28 && formState.customPickerVisual.height <= 38,
    'family picker stays a compact settings control');
  assert.notEqual(formState.customPickerVisual.radius, '0px', 'family picker uses app-theme corner treatment');
  assert.equal(formState.customPickerVisual.hasProviderLogo, true, 'family picker shows the selected provider logo');
  assert.equal(formState.customPickerVisual.hasCaret, true, 'family picker supplies its own themed caret');
  assert.equal(formState.customPickerVisual.localCompoundRow, true,
    'local provider and URL form one compact joined row instead of stacked full-width controls');
  assert.ok(formState.customPickerVisual.pickerWidthRatio < 0.65,
    'local family picker leaves most of the card row for the endpoint URL');
  assert.ok(formState.customPickerVisual.remotePickerWidth >= 126
    && formState.customPickerVisual.remotePickerWidth <= 130,
  'official remote family picker preserves the compact 128px Odysseus segment');
  assert.ok(formState.customPickerVisual.remotePickerWidthRatio < 0.4,
    'official remote provider choice is not a stock-looking full-width box');
  assert.equal(formState.customPickerVisual.remoteHintText, 'Official provider API');
  assert.equal(formState.customPickerVisual.remoteHintDisplay, 'flex');
  assert.equal(formState.customPickerVisual.remoteHintVisible, true,
    'official providers keep a visible companion route-hint segment');
  assert.ok(formState.customPickerVisual.remoteHintWidth > formState.customPickerVisual.remotePickerWidth,
    'the companion route segment occupies the remainder of the fused control');
  assert.equal(formState.quickPickerSearches, 0, 'quick provider menus stay scan-friendly without search fields');
  assert.equal(formState.cardFlowVisual.stacked, true, 'local and API setup remain distinct compact sections');
  assert.ok(formState.cardFlowVisual.headingDividers.every(style => style !== 'none'));
  assert.ok(formState.cardFlowVisual.headingIcons.every(Boolean), 'each setup section has an app-native heading icon');
  assert.ok(formState.cardFlowVisual.cardBorders.every(style => style !== 'none'), 'setup cards retain their themed frames');
  assert.ok(formState.cardFlowVisual.cardPadding.every(padding => padding >= 10), 'setup cards retain compact inset spacing');
  assert.ok(formState.ownershipLabels.length >= 2);
  assert.ok(formState.ownershipLabels.every(display => display === 'none'),
    'provider ownership metadata remains in the DOM but visually hidden');

  const remoteMenuState = await evaluate(`(() => {
    const form = document.querySelector('[data-provider-add-mode="remote"]');
    const combo = form.querySelector('.provider-control-provider-combo');
    const trigger = form.querySelector('[role="combobox"]');
    const menu = document.getElementById(trigger.getAttribute('aria-controls'));
    trigger.dispatchEvent(new KeyboardEvent('keydown', {
      key:'ArrowDown', bubbles:true, cancelable:true,
    }));
    const comboRect = combo.getBoundingClientRect();
    const triggerRect = trigger.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const menuStyle = getComputedStyle(menu);
    const result = {
      comboWidth:comboRect.width,
      triggerWidth:triggerRect.width,
      menuWidth:menuRect.width,
      innerWidth:window.innerWidth,
      clientWidth:document.documentElement.clientWidth,
      visualViewportWidth:window.visualViewport?.width || 0,
      menuStyleWidth:menuStyle.width,
      menuMaxWidth:menuStyle.maxWidth,
      menuBoxSizing:menuStyle.boxSizing,
      expectedMenuWidth:Math.min(comboRect.width, window.innerWidth - 16),
      widthDelta:Math.abs(menuRect.width - Math.min(comboRect.width, window.innerWidth - 16)),
      searchFields:menu.querySelectorAll('.provider-control-picker-search, input[type="search"]').length,
      menuClass:menu.classList.contains('provider-control-family-picker-menu'),
    };
    menu.dispatchEvent(new KeyboardEvent('keydown', {
      key:'Escape', bubbles:true, cancelable:true,
    }));
    return result;
  })()`);
  assert.ok(remoteMenuState.triggerWidth >= 126 && remoteMenuState.triggerWidth <= 130);
  assert.ok(remoteMenuState.menuWidth > 360, 'quick provider menu is no longer capped at the old 360px width');
  assert.ok(remoteMenuState.widthDelta <= 2,
    `quick provider menu width tracks the complete fused provider/route combo: ${JSON.stringify(remoteMenuState)}`);
  assert.ok(remoteMenuState.menuWidth > remoteMenuState.triggerWidth * 2,
    'quick provider menu anchors to the combo instead of the 128px trigger');
  assert.equal(remoteMenuState.searchFields, 0);
  assert.equal(remoteMenuState.menuClass, true);

  const largeCatalogState = await evaluate(`(async () => {
    const h = window.__providerAddModelsHarness;
    for (let index = 1; index <= 13; index += 1) {
      h.families.push({
        id:'remote-provider-' + String(index).padStart(2, '0'),
        display_name:'Remote Provider ' + String(index).padStart(2, '0'),
        kinds:['official'], adapters:['openai-compatible'], billing_lanes:['metered_api'],
        model_count:100 + index,
        auth_methods:[{ id:'api_key', type:'api', label:'API key' }],
      });
    }
    const providers = await import('/static/js/providerControl.js');
    await providers.load({ force:true });
    const remote = document.querySelector('[data-provider-add-mode="remote"]');
    const local = document.querySelector('[data-provider-add-mode="local"]');
    const trigger = remote.querySelector('[role="combobox"]');
    const menu = document.getElementById(trigger.getAttribute('aria-controls'));
    trigger.click();
    const search = menu.querySelector('.provider-control-picker-search');
    search.value = 'Remote Provider 13';
    search.dispatchEvent(new Event('input', { bubbles:true }));
    const visible = [...menu.querySelectorAll('[role="option"]')].filter(option => !option.hidden);
    const targetHint = visible[0]?.querySelector('.provider-control-picker-option-hint')?.textContent || '';
    visible[0]?.click();
    const selectedTriggerText = trigger.textContent.trim();
    trigger.click();
    [...menu.querySelectorAll('[role="option"]')].find(option => option.dataset.value === 'openai')?.click();
    return {
      remoteSearches:menu.querySelectorAll('.provider-control-picker-search').length,
      localSearches:local.querySelectorAll('.provider-control-picker-search').length,
      visibleMatches:visible.length,
      targetHint,
      selectedTriggerText,
      restoredTriggerText:trigger.textContent.trim(),
    };
  })()`);
  assert.equal(largeCatalogState.remoteSearches, 1, 'large remote catalogs gain one themed search field');
  assert.equal(largeCatalogState.localSearches, 0, 'small local catalogs remain scan-friendly without search');
  assert.equal(largeCatalogState.visibleMatches, 1, 'provider search filters a large catalog');
  assert.equal(largeCatalogState.targetHint, '113 models', 'model-count context stays in provider menu rows');
  assert.match(largeCatalogState.selectedTriggerText, /Remote Provider 13/);
  assert.doesNotMatch(largeCatalogState.selectedTriggerText, /113 models/, 'model counts do not clutter the compact trigger');
  assert.match(largeCatalogState.restoredTriggerText, /OpenAI/);
  assert.doesNotMatch(largeCatalogState.restoredTriggerText, /models/, 'the restored compact trigger remains terse');

  await evaluate(`(() => {
    const form = document.querySelector('[data-provider-add-mode="remote"]');
    form.querySelector('input[type="password"]').value = 'browser-test-key';
    form.requestSubmit();
  })()`);
  await waitFor(
    "window.__providerAddModelsHarness.writes.filter(item => item.kind === 'account').length === 1 && document.querySelector('[data-settings-tab=\"added-models\"]')?.classList.contains('active')",
    'simple API provider add',
  );
  const apiAddState = await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    const connectionWrite = h.writes.find(item => item.kind === 'connection');
    const accountWrite = h.writes.find(item => item.kind === 'account');
    return {
      connection:connectionWrite?.body,
      accountLabel:accountWrite?.body?.label,
      accountHasKey:Boolean(accountWrite?.body?.api_key),
      secretCleared:document.querySelector('[data-provider-add-mode="remote"] input[type="password"]')?.value === '',
      addedText:document.getElementById('provider-control-connections')?.textContent || '',
      scrolledToCreated:h.scrolledConnectionIds.includes('pc-created-1'),
    };
  })()`);
  assert.deepEqual(apiAddState.connection, {
    family_id:'openai',
    adapter_id:'openai-responses',
    kind:'official',
    billing_lane:'metered_api',
    label:'OpenAI',
    url:null,
    settings:{},
    enabled:true,
  });
  assert.equal(apiAddState.accountLabel, 'Account 1');
  assert.equal(apiAddState.accountHasKey, true);
  assert.equal(apiAddState.secretCleared, true);
  assert.equal(apiAddState.scrolledToCreated, true,
    'Add Models waits for the management snapshot before scrolling to the created connection');
  assert.match(apiAddState.addedText, /OpenAI/);
  assert.doesNotMatch(apiAddState.addedText, /GPT Test/,
    'new collapsed provider rows do not eagerly construct model bodies');

  const expansionState = await evaluate(`(() => {
    const row = document.querySelector('[data-provider-connection-id="pc-created-1"]');
    const expand = row?.querySelector('.provider-control-connection-toggle');
    if (expand?.getAttribute('aria-expanded') !== 'true') expand?.click();
    return {
      expanded:expand?.getAttribute('aria-expanded'),
      modelVisible:row?.textContent.includes('GPT Test'),
    };
  })()`);
  assert.deepEqual(expansionState, { expanded:'true', modelVisible:true },
    'an owned model connection expands in the real browser');
  await waitFor(
    "document.querySelector('[data-provider-connection-id=\"pc-created-1\"] .provider-control-models')?.dataset.healthLoaded === '1' && document.querySelectorAll('[data-provider-connection-id=\"pc-created-1\"] .provider-control-model').length === 47",
    'bounded high-cardinality model health projection',
  );
  const sharingControl = await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    h.holdOptional = true;
    const row = document.querySelector('[data-provider-connection-id="pc-created-1"]');
    const shareButton = [...(row?.querySelectorAll('button') || [])]
      .find(button => button.textContent.trim() === 'Share');
    shareButton?.click();
    return {
      exists:Boolean(shareButton),
      expanded:shareButton?.getAttribute('aria-expanded'),
      chooserBusy:row?.querySelector('.provider-control-model-share-users')?.getAttribute('aria-busy'),
    };
  })()`);
  assert.deepEqual(sharingControl, { exists:true, expanded:'true', chooserBusy:'true' },
    'the share control exposes expanded/busy state synchronously while its lazy directory is pending');
  await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    h.holdOptional = false;
    h.optionalResolvers.splice(0).forEach(resolve => resolve());
  })()`);
  await waitFor(
    "document.querySelector('[data-provider-connection-id=\"pc-created-1\"] input[aria-label$=\"with friend\"]')",
    'lazy named-user share directory',
  );
  const recipientVisible = await evaluate(`(() => {
    const recipient = document.querySelector('[data-provider-connection-id="pc-created-1"] input[aria-label$="with friend"]');
    if (!recipient) return false;
    recipient.checked = true;
    recipient.dispatchEvent(new Event('change', { bubbles:true }));
    return true;
  })()`);
  assert.equal(recipientVisible, true,
    'an owned model exposes its named-user share toggle after the lazy directory load');
  await waitFor(
    "window.__providerAddModelsHarness.writes.filter(item => item.kind === 'share').length === 1",
    'direct model share toggle',
  );
  const shareWrite = await evaluate(`(() => {
    const item = window.__providerAddModelsHarness.writes.find(entry => entry.kind === 'share');
    return item ? { modelRouteId:item.modelRouteId, recipient:item.recipient, body:item.body } : null;
  })()`);
  assert.deepEqual(shareWrite, {
    modelRouteId:'pmr-pc-created-1',
    recipient:'friend',
    body:{ enabled:true },
  }, 'sharing writes only the exact model, recipient, and desired toggle state');

  const highCardinalityRefresh = await evaluate(`(async () => {
    const row = document.querySelector('[data-provider-connection-id="pc-created-1"]');
    const settingsPanels = document.querySelector('.settings-panels');
    settingsPanels.scrollTop = Math.min(40, Math.max(0, settingsPanels.scrollHeight - settingsPanels.clientHeight));
    const settingsScrollTop = settingsPanels.scrollTop;
    const search = row?.querySelector('.provider-control-model-search');
    search.value = 'GPT Test';
    search.dispatchEvent(new Event('input', { bubbles:true }));
    search.focus();
    const before = window.__providerAddModelsHarness.requests
      .filter(item => item.pathname.includes('/eligibility')).length;
    const startedAt = performance.now();
    const providers = await import('/static/js/providerControl.js');
    await providers.load({ force:true, view:'added-models' });
    const refreshedRow = document.querySelector('[data-provider-connection-id="pc-created-1"]');
    const refreshedSearch = refreshedRow?.querySelector('.provider-control-model-search');
    return {
      modelRows:refreshedRow?.querySelectorAll('.provider-control-model').length || 0,
      eligibilityReads:window.__providerAddModelsHarness.requests
        .filter(item => item.pathname.includes('/eligibility')).length - before,
      searchValue:refreshedSearch?.value || '',
      visibleRows:[...(refreshedRow?.querySelectorAll('.provider-control-model') || [])]
        .filter(item => !item.hidden).length,
      focusPreserved:document.activeElement === refreshedSearch,
      rowIdentityPreserved:refreshedRow === row,
      settingsScrollPreserved:settingsPanels.scrollTop === settingsScrollTop,
      projectedAccountCountUsed:refreshedRow?.textContent.includes('Account connected'),
      refreshMs:performance.now() - startedAt,
    };
  })()`);
  const { refreshMs: highCardinalityRefreshMs, ...highCardinalityState } = highCardinalityRefresh;
  assert.deepEqual(
    highCardinalityState,
    {
      modelRows:47,
      eligibilityReads:1,
      searchValue:'GPT Test',
      visibleRows:1,
      focusPreserved:true,
      rowIdentityPreserved:true,
      settingsScrollPreserved:true,
      projectedAccountCountUsed:true,
    },
    'a 47-model refresh remains O(1), reconciles the existing row, and preserves search, focus, and Settings scroll',
  );
  assert.ok(highCardinalityRefreshMs <= 1000,
    `a 47-model refresh stays interactive within one second (${highCardinalityRefreshMs.toFixed(1)}ms)`);

  await evaluate(`(() => document.querySelector('[data-settings-tab="services"]').click())()`);
  await waitFor("document.querySelector('[data-provider-add-mode=\"local\"] [role=\"combobox\"]')", 'local themed provider picker');
  const pickerKeyboardState = await evaluate(`(async () => {
    const form = document.querySelector('[data-provider-add-mode="local"]');
    const trigger = form.querySelector('[role="combobox"]');
    const listbox = document.getElementById(trigger.getAttribute('aria-controls'));
    const remote = document.querySelector('[data-provider-add-mode="remote"]');
    const remoteTopBeforeOpen = remote.getBoundingClientRect().top;
    const key = (target, value) => target.dispatchEvent(new KeyboardEvent('keydown', {
      key:value, bubbles:true, cancelable:true,
    }));

    key(trigger, 'ArrowDown');
    const openedByArrow = trigger.getAttribute('aria-expanded');
    const openMenuStyle = getComputedStyle(listbox);
    const openMenuRect = listbox.getBoundingClientRect();
    const triggerRect = trigger.getBoundingClientRect();
    const overlayVisual = {
      position:openMenuStyle.position,
      zIndex:Number(openMenuStyle.zIndex || 0),
      shadow:openMenuStyle.boxShadow,
      widthCoversTrigger:openMenuRect.width >= triggerRect.width,
      optionRows:[...listbox.querySelectorAll('[role="option"]')].every(option => (
        getComputedStyle(option).display === 'flex'
        && Boolean(option.querySelector('.provider-control-picker-option-icon'))
        && Boolean(option.querySelector('.provider-control-picker-option-copy'))
      )),
      followingCardDelta:Math.abs(remote.getBoundingClientRect().top - remoteTopBeforeOpen),
    };
    const portalRect = listbox.getBoundingClientRect();
    const portalParentIsBody = listbox.parentElement === document.body;
    const portalZ = Number.parseInt(getComputedStyle(listbox).zIndex, 10) || 0;
    const modalZ = Number.parseInt(getComputedStyle(document.getElementById('settings-modal')).zIndex, 10) || 0;
    const portalInsideViewport = portalRect.left >= 0 && portalRect.top >= 0
      && portalRect.right <= window.innerWidth && portalRect.bottom <= window.innerHeight;
    key(listbox, 'End');
    const endValue = document.activeElement?.dataset?.value || '';
    key(listbox, 'Home');
    const homeValue = document.activeElement?.dataset?.value || '';
    key(listbox, 'ArrowDown');
    const arrowValue = document.activeElement?.dataset?.value || '';
    key(listbox, 'Enter');
    const selectedSecond = trigger.textContent.trim();

    key(trigger, 'ArrowDown');
    key(listbox, 'Home');
    key(listbox, 'Enter');
    const selectedOllama = trigger.textContent.trim();
    const selectedAria = [...listbox.querySelectorAll('[role="option"]')]
      .find(option => option.dataset.value === 'ollama')?.getAttribute('aria-selected');

    key(trigger, 'End');
    const expandedBeforeEscape = trigger.getAttribute('aria-expanded');
    key(listbox, 'Escape');
    return {
      openedByArrow,
      portalParentIsBody,
      portalZ,
      modalZ,
      portalInsideViewport,
      endValue,
      homeValue,
      arrowValue,
      selectedSecond,
      selectedOllama,
      selectedAria,
      expandedBeforeEscape,
      expandedAfterEscape:trigger.getAttribute('aria-expanded'),
      overlayVisual,
    };
  })()`);
  assert.equal(pickerKeyboardState.openedByArrow, 'true', 'ArrowDown opens the family listbox');
  assert.ok(['absolute', 'fixed'].includes(pickerKeyboardState.overlayVisual.position),
    'provider options render in an overlay rather than browser-native popup chrome');
  assert.ok(pickerKeyboardState.overlayVisual.zIndex >= 100);
  assert.notEqual(pickerKeyboardState.overlayVisual.shadow, 'none', 'provider menu uses app-themed overlay depth');
  assert.equal(pickerKeyboardState.overlayVisual.widthCoversTrigger, true);
  assert.equal(pickerKeyboardState.overlayVisual.optionRows, true,
    'provider choices render as composed logo/copy rows, not stock options');
  assert.ok(pickerKeyboardState.overlayVisual.followingCardDelta <= 2,
    `opening the provider picker overlays the card flow without reflowing it (delta ${pickerKeyboardState.overlayVisual.followingCardDelta}px)`);
  assert.equal(pickerKeyboardState.portalParentIsBody, true, 'open listbox escapes the scroll-clipped Settings panel');
  assert.ok(pickerKeyboardState.portalZ > pickerKeyboardState.modalZ, 'open listbox paints above the Settings modal');
  assert.equal(pickerKeyboardState.portalInsideViewport, true, 'open listbox stays inside the viewport');
  assert.equal(pickerKeyboardState.endValue, 'lmstudio', 'End moves to the final family');
  assert.equal(pickerKeyboardState.homeValue, 'ollama', 'Home moves to the first family');
  assert.equal(pickerKeyboardState.arrowValue, 'lmstudio', 'ArrowDown moves between family options');
  assert.match(pickerKeyboardState.selectedSecond, /LM Studio/, 'Enter chooses the focused family');
  assert.match(pickerKeyboardState.selectedOllama, /Ollama/, 'Ollama is selected through the custom listbox');
  assert.equal(pickerKeyboardState.selectedAria, 'true');
  assert.equal(pickerKeyboardState.expandedBeforeEscape, 'true');
  assert.equal(pickerKeyboardState.expandedAfterEscape, 'false', 'Escape closes the family listbox');
  await evaluate(`(() => document.querySelector('[data-provider-add-mode="local"]').requestSubmit())()`);
  await waitFor(
    "window.__providerAddModelsHarness.writes.filter(item => item.kind === 'connection').length === 2 && document.querySelector('[data-settings-tab=\"added-models\"]')?.classList.contains('active')",
    'simple local provider add',
  );
  const finalState = await evaluate(`(() => {
    const h = window.__providerAddModelsHarness;
    const connections = h.writes.filter(item => item.kind === 'connection');
    const providerRequests = h.requests.filter(item => item.pathname.startsWith('/api/v1/providers'));
    return {
      local:connections[1]?.body,
      accountWrites:h.writes.filter(item => item.kind === 'account').length,
      eligibilityRequests:providerRequests.filter(item => item.pathname.includes('/eligibility')).length,
      legacyRequests:h.requests.filter(item => item.pathname === '/api/model-endpoints').length,
      addedText:document.getElementById('provider-control-connections')?.textContent || '',
      connectionGroups:[...document.querySelectorAll('[data-provider-connection-group]')].map(group => group.dataset.providerConnectionGroup).sort(),
      connectionRows:document.querySelectorAll('[data-provider-connection-id]').length,
      compactRows:[...document.querySelectorAll('[data-provider-connection-id]')].every(row => Boolean(row.querySelector('.provider-control-connection-toggle'))),
      expandedIds:[...document.querySelectorAll('[data-provider-connection-id]')]
        .filter(row => row.querySelector('.provider-control-connection-toggle')?.getAttribute('aria-expanded') === 'true')
        .map(row => row.dataset.providerConnectionId),
      nativeSelects:document.querySelectorAll('[data-settings-panel="services"] select, [data-settings-panel="added-models"] select').length,
      compactRowVisual:[...document.querySelectorAll('[data-provider-connection-id]')]
        .filter(row => row.querySelector('.provider-control-connection-toggle')?.getAttribute('aria-expanded') === 'false')
        .map(row => {
        const toggle = row.querySelector('.provider-control-connection-toggle');
        const rowRect = row.getBoundingClientRect();
        const toggleRect = toggle?.getBoundingClientRect();
        return {
          height:rowRect.height,
          toggleDisplay:toggle ? getComputedStyle(toggle).display : '',
          toggleWidthRatio:toggleRect && rowRect.width ? toggleRect.width / rowRect.width : 0,
          hasLogo:Boolean(row.querySelector('.provider-control-connection-logo svg')),
          hasBadges:Boolean(row.querySelector('.provider-control-badges .admin-badge')),
          hasCaret:Boolean(row.querySelector('.provider-control-connection-chevron svg')),
          actionsInline:Boolean(row.querySelector('.provider-control-connection-actions')),
        };
        }),
      groupHeadingsUppercase:[...document.querySelectorAll('.provider-control-group-heading')]
        .every(heading => getComputedStyle(heading).textTransform === 'uppercase'),
      browserErrors:[...h.errors],
      providerRequestCount:providerRequests.length,
    };
  })()`);
  assert.deepEqual(finalState.local, {
    family_id:'ollama',
    adapter_id:'ollama',
    kind:'local',
    billing_lane:'local',
    label:'Ollama',
    url:'http://localhost:11434',
    settings:{},
    enabled:true,
  });
  assert.equal(finalState.accountWrites, 1, 'keyless local add does not create a credential account');
  assert.equal(finalState.eligibilityRequests, 3,
    'health stays bounded to one read per expansion/explicit/mutation refresh wave');
  assert.equal(finalState.legacyRequests, 0, 'normalized UI never calls the retired model-endpoints API');
  assert.match(finalState.addedText, /Ollama/);
  assert.match(finalState.addedText, /GPT Test/,
    'the previously expanded provider body remains materialized after refresh');
  assert.deepEqual(finalState.connectionGroups, ['api', 'local'], 'Added Models groups compact rows by API and local connections');
  assert.equal(finalState.connectionRows, 3);
  assert.equal(finalState.compactRows, true, 'every Added Models row has a compact expand button');
  assert.deepEqual(finalState.expandedIds, ['pc-created-1'],
    'refresh preserves the provider row the user expanded');
  assert.equal(finalState.nativeSelects, 0, 'normalized provider UI never renders native selects');
  assert.ok(finalState.compactRowVisual.every(row => (
    row.height < 100
    && row.toggleDisplay === 'flex'
    && row.toggleWidthRatio > 0.55
    && row.hasLogo
    && row.hasBadges
    && row.hasCaret
    && row.actionsInline
  )), 'collapsed Added Models entries remain compact composed provider rows');
  assert.equal(finalState.groupHeadingsUppercase, true, 'Local/API groups use the app settings label treatment');
  assert(finalState.providerRequestCount > 0);
  assert.deepEqual(finalState.browserErrors, []);
  assert.deepEqual(runtimeErrors, []);
  assert.deepEqual(consoleErrors, []);

  process.stdout.write(JSON.stringify({
    addedModelsVisible:true,
    progressiveCoreLoad:true,
    forceReplacementAbort:true,
    singleInvalidationReload:true,
    apiAdd:true,
    localAdd:true,
    directModelShare:true,
    highCardinalityRefresh:true,
    legacyRequests:finalState.legacyRequests,
    eligibilityRequests:finalState.eligibilityRequests,
    addOpenCoreReads:{
      connections:catalogFirstState.connectionReads,
      models:catalogFirstState.modelReads,
    },
    lazyAiCatalogReads:afterAi.catalog - beforeAi.catalog,
    firstUsablePaintMs:catalogFirstState.firstUsablePaintMs,
    corePaintMs:progressiveState.corePaintMs,
    highCardinalityRefreshMs,
    providerRequestCount:finalState.providerRequestCount,
  }) + '\n');
} finally {
  if (socket) socket.close();
  const exited = new Promise(resolve => browser.once('exit', resolve));
  browser.kill('SIGTERM');
  await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 3000))]);
  if (browser.exitCode === null) browser.kill('SIGKILL');
  await closeServer(server);
  fs.rmSync(profile, { recursive:true, force:true });
}
