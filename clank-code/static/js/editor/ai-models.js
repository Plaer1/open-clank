import { editorIdSelector } from './dom-identity.js';
/**
 * Managed image-route dropdown loader.
 *
 * Values are stable ProviderModelRoute IDs from `/api/v1/providers/models`.
 * The browser never receives or submits provider URLs, keys, or adapter
 * details. A blank value means "use the owner's ordered Images binding".
 */

const TOOL_OPERATIONS = Object.freeze({
  harmonize: ['image.img2img', 'image.edit'],
  style: ['image.img2img', 'image.edit'],
  inpaint: ['image.inpaint'],
  upscale: ['image.upscale'],
  denoise: ['image.denoise'],
  rembg: ['image.remove_background'],
  segment: ['image.segment'],
  'enhance-face': ['image.restore_face'],
});

function operations(route) {
  return new Set(Array.isArray(route?.operations) ? route.operations : []);
}

function supports(route, expected) {
  if (!route || route.enabled === false) return false;
  const available = operations(route);
  return expected.some(operation => available.has(operation));
}

function routeLabel(route) {
  const display = String(route.display_name || route.model_id || route.id || 'Image model');
  const providerModel = String(route.model_id || '');
  return providerModel && providerModel !== display
    ? `${display} · ${providerModel}`
    : display;
}

function setOptions(select, routes, emptyLabel) {
  const previous = select?.value || '';
  if (!select) return;
  select.replaceChildren();
  const automatic = select.ownerDocument.createElement('option');
  automatic.value = '';
  automatic.textContent = emptyLabel;
  select.appendChild(automatic);
  for (const route of routes) {
    const option = select.ownerDocument.createElement('option');
    option.value = route.id;
    option.textContent = routeLabel(route);
    select.appendChild(option);
  }
  if (previous && routes.some(route => route.id === previous)) {
    select.value = previous;
  }
}

function showUnavailable(select, label = 'No managed image route configured') {
  if (!select) return;
  select.replaceChildren();
  const automatic = select.ownerDocument.createElement('option');
  automatic.value = '';
  automatic.textContent = 'Auto';
  select.appendChild(automatic);
  const unavailable = select.ownerDocument.createElement('option');
  unavailable.disabled = true;
  unavailable.textContent = label;
  select.appendChild(unavailable);
}

export function wireAIModelSelectors({ container, apiBase, lifetime = null, fetchImpl = globalThis.fetch }) {
  const doc = container.ownerDocument;
  const find = id => container.querySelector(editorIdSelector(id));
  const selects = () => container.querySelectorAll('select.ge-tool-model');
  const removers = []; let disposed = false, sequence = 0, controller = null;
  const listen = (node, type, fn, options) => { node.addEventListener(type, fn, options); removers.push(() => node.removeEventListener(type, fn, options)); };
  const dispose = () => { if (disposed) return; disposed = true; sequence++; controller?.abort(); for (const remove of removers.splice(0)) remove(); };
  lifetime?.add(dispose);
  const aiGenSelect = find('ge-ai-model');
  const aiInpaintSelect = find('ge-ai-inpaint');
  if (!aiGenSelect && !aiInpaintSelect &&
      !container.querySelector('select.ge-tool-model')) return;

  let loading = null;
  async function loadAIModels() {
    if (disposed) return;
    if (loading) return loading;
    const request = ++sequence, owner = doc.body.dataset.accountId;
    controller = new AbortController();
    const valid = () => !disposed && !lifetime?.disposed && sequence === request && doc.body.dataset.accountId === owner;
    loading = (async () => {
      try {
        const response = await fetchImpl(`${apiBase}/api/v1/providers/models`, {
          credentials: 'same-origin', signal: controller.signal,
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error(`Provider routes unavailable (${response.status})`);
        const payload = await response.json();
        if (!valid()) return;
        const routes = Array.isArray(payload.models) ? payload.models : [];

        setOptions(
          aiGenSelect,
          routes.filter(route => supports(route, ['image.generate'])),
          'Auto',
        );
        setOptions(
          aiInpaintSelect,
          routes.filter(route => supports(route, TOOL_OPERATIONS.inpaint)),
          'Auto',
        );

        for (const select of selects()) {
          const tool = String(select.dataset.geToolModel || '');
          const expected = TOOL_OPERATIONS[tool] || [
            'image.edit',
            'image.img2img',
            'image.upscale',
            'image.denoise',
            'image.remove_background',
            'image.restore_face',
          ];
          setOptions(
            select,
            routes.filter(route => supports(route, expected)),
            'Auto',
          );
          const storageKey = `ge-tool-model-route-${tool}`;
          try {
            const saved = localStorage.getItem(storageKey);
            if (saved && [...select.options].some(option => option.value === saved)) {
              select.value = saved;
            }
          } catch (_) {}
          if (!select.dataset.managedRouteListener) {
            select.dataset.managedRouteListener = '1';
            removers.push(() => { delete select.dataset.managedRouteListener; });
            listen(select, 'change', () => {
              try {
                if (select.value) localStorage.setItem(storageKey, select.value);
                else localStorage.removeItem(storageKey);
              } catch (_) {}
            });
          }
        }
      } catch (_) {
        if (!valid()) return;
        showUnavailable(aiGenSelect);
        showUnavailable(aiInpaintSelect);
        selects().forEach(select => {
          showUnavailable(select);
        });
      } finally {
        loading = null;
      }
    })();
    return loading;
  }

  loadAIModels();

  const onProvidersUpdated = () => {
    if (!container.isConnected) {
      dispose();
      return;
    }
    loadAIModels();
  };
  listen(doc, 'open-clank:providers-updated', onProvidersUpdated);
  listen(doc, 'openclank:auth-context-changed', dispose);

  let lastRefresh = 0;
  listen(container, 'mousedown', event => {
    const select = event.target.closest(
      ':is(#ge-ai-model,[data-ge-id="ge-ai-model"]), :is(#ge-ai-inpaint,[data-ge-id="ge-ai-inpaint"]), select.ge-tool-model',
    );
    if (!select) return;
    const now = Date.now();
    if (now - lastRefresh < 3000) return;
    lastRefresh = now;
    loadAIModels();
  }, true);
  return { dispose, loadAIModels };
}
