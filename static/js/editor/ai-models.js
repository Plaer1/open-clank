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
  const automatic = document.createElement('option');
  automatic.value = '';
  automatic.textContent = emptyLabel;
  select.appendChild(automatic);
  for (const route of routes) {
    const option = document.createElement('option');
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
  const automatic = document.createElement('option');
  automatic.value = '';
  automatic.textContent = 'Auto';
  select.appendChild(automatic);
  const unavailable = document.createElement('option');
  unavailable.disabled = true;
  unavailable.textContent = label;
  select.appendChild(unavailable);
}

export function wireAIModelSelectors({ container, apiBase }) {
  const aiGenSelect = document.getElementById('ge-ai-model');
  const aiInpaintSelect = document.getElementById('ge-ai-inpaint');
  if (!aiGenSelect && !aiInpaintSelect &&
      !document.querySelector('select.ge-tool-model')) return;

  let loading = null;
  async function loadAIModels() {
    if (loading) return loading;
    loading = (async () => {
      try {
        const response = await fetch(`${apiBase}/api/v1/providers/models`, {
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) throw new Error(`Provider routes unavailable (${response.status})`);
        const payload = await response.json();
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

        for (const select of document.querySelectorAll('select.ge-tool-model')) {
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
            select.addEventListener('change', () => {
              try {
                if (select.value) localStorage.setItem(storageKey, select.value);
                else localStorage.removeItem(storageKey);
              } catch (_) {}
            });
          }
        }
      } catch (_) {
        showUnavailable(aiGenSelect);
        showUnavailable(aiInpaintSelect);
        document.querySelectorAll('select.ge-tool-model').forEach(select => {
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
      document.removeEventListener('open-clank:providers-updated', onProvidersUpdated);
      return;
    }
    loadAIModels();
  };
  document.addEventListener('open-clank:providers-updated', onProvidersUpdated);

  let lastRefresh = 0;
  container.addEventListener('mousedown', event => {
    const select = event.target.closest(
      '#ge-ai-model, #ge-ai-inpaint, select.ge-tool-model',
    );
    if (!select) return;
    const now = Date.now();
    if (now - lastRefresh < 3000) return;
    lastRefresh = now;
    loadAIModels();
  }, true);
}
