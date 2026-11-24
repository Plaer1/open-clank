// compare/probe.js — model probe/check system
import state from './state.js';
import { WAVE_FRAMES } from './icons.js';
import uiModule from '../ui.js';
import spinnerModule from '../spinner.js';

function _clearProbeWaves() {
  const rows = document.querySelectorAll('.compare-probe-row');
  rows.forEach(r => { if (r._waveInterval) { clearInterval(r._waveInterval); r._waveInterval = null; } });
}

async function _checkUnprobed() {
  const unprobed = state._selectedModels.filter(m => !state._probed.has(m.model));
  if (unprobed.length === 0) {
    if (uiModule) uiModule.showToast('All models verified');
    return;
  }

  // Whirlpool loader on the Probe button while the check runs.
  const _btn = document.getElementById('compare-check-btn');
  let _btnHTML = null, _wp = null;
  if (_btn) {
    _btnHTML = _btn.innerHTML;
    _btn.disabled = true;
    _btn.style.opacity = '0.7';
    try {
      _wp = spinnerModule.createWhirlpool(14);
      _btn.innerHTML = '';
      _btn.appendChild(_wp.element);
    } catch (_) { /* spinner best-effort */ }
  }

  // Quick inline probe — show toast with results
  const isBlind = state._blindMode;
  let ok = 0, fail = 0;
  try {
  let catalog = { items: [] };
  try {
    const response = await fetch(`${state.API_BASE}/api/models`, { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Provider catalog unavailable');
    catalog = await response.json();
  } catch (_) {
    catalog = { items: [] };
  }
  for (const m of unprobed) {
    try {
      const _imageModelPrefixes = ['dall-e', 'gpt-image', 'chatgpt-image', 'stable-diffusion', 'sdxl', 'flux', 'midjourney'];
      if (_imageModelPrefixes.some(p => m.model.toLowerCase().includes(p))) {
        state._probed.add(m.model);
        ok++;
        continue;
      }
      const available = (catalog.items || []).some(item =>
        item.endpoint_id === (m.endpointId || '')
        && ((item.models || []).includes(m.model)
          || (item.catalog || []).some(entry => entry.model_id === m.model))
      );
      if (available) {
        state._probed.add(m.model);
        ok++;
      } else {
        fail++;
        const name = isBlind ? 'a model' : (m.name || m.model.split('/').pop());
        if (uiModule) uiModule.showToast(`${name} is no longer in the managed provider catalog`, 5000);
      }
    } catch (e) {
      fail++;
    }
  }
  if (fail === 0) {
    if (uiModule) uiModule.showToast(`${ok} model${ok > 1 ? 's' : ''} verified`);
  }
  } finally {
    // Restore the Probe button (its label/visibility is refreshed below).
    if (_btn) {
      _btn.disabled = false;
      _btn.style.opacity = '';
      if (_btnHTML !== null) _btn.innerHTML = _btnHTML;
    }
    if (window._updateCheckBtnState) window._updateCheckBtnState();
  }
}

export { _clearProbeWaves, _checkUnprobed };
